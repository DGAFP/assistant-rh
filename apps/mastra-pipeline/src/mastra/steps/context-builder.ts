import { createStep } from "@mastra/core/workflows";
import { z } from "zod";
import {
	DEFAULT_RUNTIME_RAG_CONFIG,
	getRuntimeRagConfig,
	type RuntimeRagConfig,
} from "../lib/config";
import { getDbPool } from "../lib/db";
import {
	contextBuildConfigSchema,
	contextSelectorStateSchema,
	contextSelectorStepInputSchema,
} from "./context-selector";
import { retrieverStepInputSchema } from "./retriever";
import { aggregatedSectionSchema, sectionAggregatorStepInputSchema } from "./section-aggregator";

interface NormalizedContextBuildConfig {
	context_mode: "standard" | "wide";
	token_budget: number;
	max_full_docs: number;
	doc_entire_threshold: number;
	max_sections: number;
	triangulation_sections: number;
	legal_refs_budget: number;
	token_budget_wide: number;
	max_full_docs_wide: number;
	doc_entire_threshold_wide: number;
	max_sections_wide: number;
	legal_refs_budget_wide: number;
}

interface FullDocumentRow {
	doc_id: string;
	title: string | null;
	source_url: string | null;
	publisher: string | null;
	doc_markdown: string | null;
	token_count: number | null;
}

interface LegalReferenceRow {
	number: string | null;
	cid: string | null;
	url: string | null;
	full_title: string | null;
}

const contextItemSchema = z.object({
	sectionId: z.string().nullable(),
	heading: z.string(),
	content: z.string(),
	score: z.number(),
	publisher: z.string().nullable(),
	documentTitle: z.string().nullable(),
	documentUrl: z.string().nullable(),
	referencesJuridiques: z.unknown().nullable(),
	tokenEstimate: z.number().int().nonnegative(),
	metadata: z.record(z.string(), z.unknown()),
});

const contextMetaSchema = z.object({
	contextMode: z.enum(["standard", "wide"]),
	tokenBudget: z.number().int().positive(),
	tokenCount: z.number().int().nonnegative(),
	refsTokenCount: z.number().int().nonnegative(),
	maxSections: z.number().int().positive(),
	selectedCount: z.number().int().nonnegative(),
	fullDocCount: z.number().int().nonnegative(),
	triangulationAdded: z.number().int().nonnegative(),
	legalRefsResolvedCount: z.number().int().nonnegative(),
	legalRefsInjectedCount: z.number().int().nonnegative(),
	lastResolvedRefs: z.record(z.string(), z.record(z.string(), z.string())),
	warnings: z.array(z.string()),
});

export const contextBuilderStepInputSchema = z.object({
	sections: z.array(aggregatedSectionSchema),
	config: contextBuildConfigSchema.optional(),
});

export const contextBuilderStepOutputSchema = z.object({
	contextItems: z.array(contextItemSchema),
	context: z.string(),
	contextMeta: contextMetaSchema,
});

export const contextBuilderStateSchema = contextSelectorStateSchema.extend({
	config: z
		.object({
			retrieval: retrieverStepInputSchema.shape.config.optional(),
			aggregation: sectionAggregatorStepInputSchema.shape.config.optional(),
			selector: contextSelectorStepInputSchema.shape.config.optional(),
			context: contextBuildConfigSchema.optional(),
		})
		.passthrough()
		.optional(),
	contextBuilder: contextBuilderStepOutputSchema.optional(),
});

type ContextBuilderInput = z.infer<typeof contextBuilderStepInputSchema>;
type ContextBuilderOutput = z.infer<typeof contextBuilderStepOutputSchema>;
type ContextBuildConfigInput = z.infer<typeof contextBuildConfigSchema>;
type ContextItem = z.infer<typeof contextItemSchema>;
type AggregatedSection = z.infer<typeof aggregatedSectionSchema>;

function estimateTokens(text: string): number {
	return Math.floor((text ?? "").length / 4);
}

function normalizeContextBuildConfig(
	runtimeConfig: RuntimeRagConfig["context"],
	override?: ContextBuildConfigInput,
): NormalizedContextBuildConfig {
	const base: NormalizedContextBuildConfig = {
		context_mode: "standard",
		token_budget: 8000,
		max_full_docs: 1,
		doc_entire_threshold: 3500,
		max_sections: 12,
		triangulation_sections: 2,
		legal_refs_budget: 1000,
		token_budget_wide: 12000,
		max_full_docs_wide: 2,
		doc_entire_threshold_wide: 5000,
		max_sections_wide: 20,
		legal_refs_budget_wide: 2000,
	};

	return {
		context_mode: override?.context_mode ?? runtimeConfig?.context_mode ?? base.context_mode,
		token_budget: override?.token_budget ?? runtimeConfig?.token_budget ?? base.token_budget,
		max_full_docs: override?.max_full_docs ?? runtimeConfig?.max_full_docs ?? base.max_full_docs,
		doc_entire_threshold:
			override?.doc_entire_threshold ??
			runtimeConfig?.doc_entire_threshold ??
			base.doc_entire_threshold,
		max_sections: override?.max_sections ?? runtimeConfig?.max_sections ?? base.max_sections,
		triangulation_sections:
			override?.triangulation_sections ??
			runtimeConfig?.triangulation_sections ??
			base.triangulation_sections,
		legal_refs_budget:
			override?.legal_refs_budget ?? runtimeConfig?.legal_refs_budget ?? base.legal_refs_budget,
		token_budget_wide:
			override?.token_budget_wide ?? runtimeConfig?.token_budget_wide ?? base.token_budget_wide,
		max_full_docs_wide:
			override?.max_full_docs_wide ?? runtimeConfig?.max_full_docs_wide ?? base.max_full_docs_wide,
		doc_entire_threshold_wide:
			override?.doc_entire_threshold_wide ??
			runtimeConfig?.doc_entire_threshold_wide ??
			base.doc_entire_threshold_wide,
		max_sections_wide:
			override?.max_sections_wide ?? runtimeConfig?.max_sections_wide ?? base.max_sections_wide,
		legal_refs_budget_wide:
			override?.legal_refs_budget_wide ??
			runtimeConfig?.legal_refs_budget_wide ??
			base.legal_refs_budget_wide,
	};
}

async function loadRuntimeContextBuildConfigSafe(): Promise<RuntimeRagConfig["context"]> {
	try {
		const runtimeConfig = await getRuntimeRagConfig();
		return runtimeConfig.context;
	} catch {
		return DEFAULT_RUNTIME_RAG_CONFIG.context;
	}
}

function resolveContextModeValues(config: NormalizedContextBuildConfig): {
	mode: "standard" | "wide";
	tokenBudget: number;
	maxFullDocs: number;
	docEntireThreshold: number;
	maxSections: number;
	legalRefsBudget: number;
} {
	if (config.context_mode === "wide") {
		return {
			mode: "wide",
			tokenBudget: config.token_budget_wide,
			maxFullDocs: config.max_full_docs_wide,
			docEntireThreshold: config.doc_entire_threshold_wide,
			maxSections: config.max_sections_wide,
			legalRefsBudget: config.legal_refs_budget_wide,
		};
	}

	return {
		mode: "standard",
		tokenBudget: config.token_budget,
		maxFullDocs: config.max_full_docs,
		docEntireThreshold: config.doc_entire_threshold,
		maxSections: config.max_sections,
		legalRefsBudget: config.legal_refs_budget,
	};
}

async function loadFullDocument(docId: string): Promise<FullDocumentRow | null> {
	const db = getDbPool();
	const result = await db.query<FullDocumentRow>(
		`
      SELECT
        doc_id::text AS doc_id,
        title,
        source_url,
        publisher,
        doc_markdown,
        token_count
      FROM rag_documents
      WHERE doc_id = $1::uuid
        AND doc_markdown IS NOT NULL
      LIMIT 1
    `,
		[docId],
	);

	if (result.rowCount === 0) {
		return null;
	}

	return result.rows[0] ?? null;
}

function asNumber(value: unknown, fallback = 0): number {
	if (typeof value === "number" && Number.isFinite(value)) {
		return value;
	}

	const parsed = Number.parseFloat(String(value));
	return Number.isFinite(parsed) ? parsed : fallback;
}

function asNullableString(value: unknown): string | null {
	if (typeof value !== "string") {
		return null;
	}

	const normalized = value.trim();
	return normalized.length > 0 ? normalized : null;
}

function toSectionContextItem(section: AggregatedSection): ContextItem {
	return {
		sectionId: section.sectionId,
		heading: section.heading,
		content: section.markdown,
		score: section.score,
		publisher: section.publisher,
		documentTitle: asNullableString(section.metadata.doc_title),
		documentUrl: asNullableString(section.metadata.doc_url),
		referencesJuridiques: section.referencesJuridiques,
		tokenEstimate: estimateTokens(section.markdown),
		metadata: section.metadata,
	};
}

function toFullDocContextItem(
	docRow: FullDocumentRow,
	matchedSections: AggregatedSection[],
): ContextItem {
	const bestScore = Math.max(...matchedSections.map((section) => section.score));

	const allReferences = matchedSections
		.map((section) => section.referencesJuridiques)
		.filter((refs) => refs !== null && refs !== undefined);

	const markdown = docRow.doc_markdown ?? "";
	return {
		sectionId: null,
		heading: docRow.title ?? "",
		content: markdown,
		score: bestScore,
		publisher: docRow.publisher,
		documentTitle: docRow.title,
		documentUrl: docRow.source_url,
		referencesJuridiques: allReferences[0] ?? null,
		tokenEstimate: docRow.token_count ?? estimateTokens(markdown),
		metadata: {
			doc_id: docRow.doc_id,
			doc_title: docRow.title ?? "",
			doc_url: docRow.source_url ?? "",
			doc_publisher: docRow.publisher ?? "",
			doc_token_count: docRow.token_count ?? 0,
		},
	};
}

function collectReferenceNumbers(items: ContextItem[]): string[] {
	const numbers = new Set<string>();

	for (const item of items) {
		const refs = item.referencesJuridiques;
		if (!refs) {
			continue;
		}

		let parsed: unknown = refs;
		if (typeof refs === "string") {
			try {
				parsed = JSON.parse(refs);
			} catch {
				continue;
			}
		}

		if (!Array.isArray(parsed)) {
			continue;
		}

		for (const entry of parsed) {
			if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
				continue;
			}

			const number = asNullableString((entry as Record<string, unknown>).number);
			if (number) {
				numbers.add(number);
			}
		}
	}

	return Array.from(numbers);
}

async function resolveLegalReferenceCids(
	numbers: string[],
): Promise<Record<string, { cid: string; url: string; title: string }>> {
	if (numbers.length === 0) {
		return {};
	}

	const db = getDbPool();
	const result = await db.query<LegalReferenceRow>(
		`
      SELECT
        number,
        cid,
        url,
        full_title
      FROM rag_chunks_dgafp
      WHERE number = ANY($1::text[])
      GROUP BY number, cid, url, full_title
    `,
		[numbers],
	);

	const out: Record<string, { cid: string; url: string; title: string }> = {};
	for (const row of result.rows) {
		const number = asNullableString(row.number);
		const cid = asNullableString(row.cid);

		if (!number || !cid) {
			continue;
		}

		out[number] = {
			cid,
			url: asNullableString(row.url) ?? "",
			title: asNullableString(row.full_title) ?? "",
		};
	}

	return out;
}

function enrichReferencesWithCid(
	refs: unknown,
	cidMap: Record<string, { cid: string; url: string; title: string }>,
): unknown {
	let parsed: unknown = refs;

	if (typeof parsed === "string") {
		try {
			parsed = JSON.parse(parsed);
		} catch {
			return refs;
		}
	}

	if (!Array.isArray(parsed)) {
		return refs;
	}

	const enriched = parsed.map((entry) => {
		if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
			return entry;
		}

		const item = { ...(entry as Record<string, unknown>) };
		const number = asNullableString(item.number);

		if (number && cidMap[number]) {
			item.cid = cidMap[number].cid;
			item.url = cidMap[number].url;
			item.title = cidMap[number].title;
		}

		return item;
	});

	return enriched;
}

function formatReferences(refs: unknown): string {
	if (typeof refs === "string") {
		return refs;
	}

	if (refs && typeof refs === "object" && !Array.isArray(refs)) {
		return Object.entries(refs)
			.map(([key, value]) => `- ${key}: ${String(value)}`)
			.join("\n");
	}

	if (Array.isArray(refs)) {
		return refs.map((entry) => `- ${JSON.stringify(entry)}`).join("\n");
	}

	return String(refs ?? "");
}

export function formatContextForPrompt(items: ContextItem[]): string {
	return items
		.map((item, idx) => {
			let header = `[Source ${idx + 1}] ${item.documentTitle || item.heading}`;
			if (item.publisher) {
				header += ` (${item.publisher})`;
			}

			return `### ${header}\n\n\`\`\`markdown\n${item.content}\n\`\`\`\n\n---\n`;
		})
		.join("\n");
}

export async function runContextBuilder(
	input: ContextBuilderInput,
	runtimeContextConfigOverride?: RuntimeRagConfig["context"],
): Promise<ContextBuilderOutput> {
	const runtimeContextConfig =
		runtimeContextConfigOverride ?? (await loadRuntimeContextBuildConfigSafe());
	const config = normalizeContextBuildConfig(runtimeContextConfig, input.config);
	const modeValues = resolveContextModeValues(config);

	if (input.sections.length === 0) {
		return {
			contextItems: [],
			context: "",
			contextMeta: {
				contextMode: modeValues.mode,
				tokenBudget: modeValues.tokenBudget,
				tokenCount: 0,
				refsTokenCount: 0,
				maxSections: modeValues.maxSections,
				selectedCount: 0,
				fullDocCount: 0,
				triangulationAdded: 0,
				legalRefsResolvedCount: 0,
				legalRefsInjectedCount: 0,
				lastResolvedRefs: {},
				warnings: [],
			},
		};
	}

	const selected: ContextItem[] = [];
	const usedIds = new Set<string>();
	let tokensUsed = 0;
	let fullDocCount = 0;
	let triangulationAdded = 0;
	let refsTokenCount = 0;
	let legalRefsInjectedCount = 0;
	const warnings: string[] = [];

	const byDoc = new Map<string, AggregatedSection[]>();
	const standalone: AggregatedSection[] = [];

	for (const section of input.sections) {
		if (section.documentId) {
			const existing = byDoc.get(section.documentId);
			if (existing) {
				existing.push(section);
			} else {
				byDoc.set(section.documentId, [section]);
			}
			continue;
		}

		standalone.push(section);
	}

	const sortedDocs = Array.from(byDoc.entries()).sort((left, right) => {
		const leftScore = Math.max(...left[1].map((section) => section.score));
		const rightScore = Math.max(...right[1].map((section) => section.score));
		return rightScore - leftScore;
	});

	for (const [docId, docSections] of sortedDocs) {
		if (fullDocCount >= modeValues.maxFullDocs) {
			break;
		}

		const docTokenCount = asNumber(docSections[0]?.metadata.doc_token_count, 0);
		if (docTokenCount <= 0 || docTokenCount > modeValues.docEntireThreshold) {
			continue;
		}

		if (tokensUsed + docTokenCount > modeValues.tokenBudget) {
			continue;
		}

		try {
			const docRow = await loadFullDocument(docId);
			if (!docRow?.doc_markdown) {
				continue;
			}

			const fullDocItem = toFullDocContextItem(docRow, docSections);
			fullDocItem.metadata.is_doc_entire = true;

			selected.push(fullDocItem);
			for (const section of docSections) {
				usedIds.add(section.sectionId || section.heading);
			}

			tokensUsed += fullDocItem.tokenEstimate;
			fullDocCount += 1;
		} catch (error) {
			const message = error instanceof Error ? error.message : "Unknown full-doc load error";
			warnings.push(`Failed to load full doc ${docId}: ${message}`);
		}
	}

	for (const section of input.sections) {
		const key = section.sectionId || section.heading;
		if (usedIds.has(key)) {
			continue;
		}

		const item = toSectionContextItem(section);
		if (tokensUsed + item.tokenEstimate > modeValues.tokenBudget) {
			continue;
		}

		if (selected.length >= modeValues.maxSections) {
			break;
		}

		selected.push(item);
		usedIds.add(key);
		tokensUsed += item.tokenEstimate;
	}

	for (const section of standalone) {
		const key = section.sectionId || section.heading || String(selected.length);
		if (usedIds.has(key)) {
			continue;
		}

		const item = toSectionContextItem(section);
		if (tokensUsed + item.tokenEstimate > modeValues.tokenBudget) {
			continue;
		}

		if (selected.length >= modeValues.maxSections) {
			break;
		}

		selected.push(item);
		usedIds.add(key);
		tokensUsed += item.tokenEstimate;
	}

	const primaryPublisher = selected[0]?.publisher;
	for (const section of input.sections) {
		if (triangulationAdded >= config.triangulation_sections) {
			break;
		}

		if (section.publisher === primaryPublisher) {
			continue;
		}

		const key = section.sectionId || section.heading;
		if (usedIds.has(key)) {
			continue;
		}

		const item = toSectionContextItem(section);
		if (tokensUsed + item.tokenEstimate > modeValues.tokenBudget) {
			continue;
		}

		selected.push(item);
		usedIds.add(key);
		tokensUsed += item.tokenEstimate;
		triangulationAdded += 1;
	}

	const allRefNumbers = collectReferenceNumbers(selected);

	let cidMap: Record<string, { cid: string; url: string; title: string }> = {};
	try {
		cidMap = await resolveLegalReferenceCids(allRefNumbers);
	} catch (error) {
		const message = error instanceof Error ? error.message : "Unknown CID resolution error";
		warnings.push(`CID resolution failed: ${message}`);
	}

	for (const item of selected) {
		if (!item.referencesJuridiques || refsTokenCount >= modeValues.legalRefsBudget) {
			continue;
		}

		item.referencesJuridiques = enrichReferencesWithCid(item.referencesJuridiques, cidMap);
		const refText = formatReferences(item.referencesJuridiques);
		const refTokens = estimateTokens(refText);

		if (
			refsTokenCount + refTokens > modeValues.legalRefsBudget ||
			tokensUsed + refTokens > modeValues.tokenBudget
		) {
			continue;
		}

		item.content += `\n\n---\nReferences juridiques :\n${refText}`;
		item.tokenEstimate += refTokens;
		refsTokenCount += refTokens;
		tokensUsed += refTokens;
		legalRefsInjectedCount += 1;
	}

	return {
		contextItems: selected,
		context: formatContextForPrompt(selected),
		contextMeta: {
			contextMode: modeValues.mode,
			tokenBudget: modeValues.tokenBudget,
			tokenCount: tokensUsed,
			refsTokenCount,
			maxSections: modeValues.maxSections,
			selectedCount: selected.length,
			fullDocCount,
			triangulationAdded,
			legalRefsResolvedCount: Object.keys(cidMap).length,
			legalRefsInjectedCount,
			lastResolvedRefs: cidMap,
			warnings,
		},
	};
}

export const contextBuilderStep = createStep({
	id: "context-builder",
	description: "Build final context items and prompt payload from selected sections",
	inputSchema: contextBuilderStepInputSchema,
	outputSchema: contextBuilderStepOutputSchema,
	stateSchema: contextBuilderStateSchema,
	execute: async ({ inputData, state, setState }) => {
		const runtimeContextConfig = await loadRuntimeContextBuildConfigSafe();
		const result = await runContextBuilder(inputData, runtimeContextConfig);

		await setState({
			...state,
			contextBuilder: result,
		});

		return result;
	},
});
