import assert from "node:assert/strict";
import type { z } from "zod";
import { DEFAULT_RUNTIME_RAG_CONFIG } from "../src/mastra/lib/config";
import type { contextBuilderStepOutputSchema } from "../src/mastra/steps/context-builder";
import type { contextSelectorStepOutputSchema } from "../src/mastra/steps/context-selector";
import type { generatorStepOutputSchema } from "../src/mastra/steps/generator";
import type { queryProcessorStepOutputSchema } from "../src/mastra/steps/query-processor";
import type { retrievedChunkSchema } from "../src/mastra/steps/retriever";
import type {
	aggregatedSectionSchema,
	sectionAggregatorStepOutputSchema,
} from "../src/mastra/steps/section-aggregator";
import type { RagPipelineExecutionDependencies } from "../src/mastra/workflows/rag-pipeline";
import { runRagPipelineRagBranch } from "../src/mastra/workflows/rag-pipeline";

type QueryProcessorOutput = z.infer<typeof queryProcessorStepOutputSchema>;
type RetrievedChunk = z.infer<typeof retrievedChunkSchema>;
type AggregatedSection = z.infer<typeof aggregatedSectionSchema>;
type RetrieverOutput = Awaited<ReturnType<RagPipelineExecutionDependencies["runRetriever"]>>;
type SectionAggregatorOutput = z.infer<typeof sectionAggregatorStepOutputSchema>;
type ContextSelectorOutput = z.infer<typeof contextSelectorStepOutputSchema>;
type ContextBuilderOutput = z.infer<typeof contextBuilderStepOutputSchema>;
type GeneratorOutput = z.infer<typeof generatorStepOutputSchema>;

const inputData: QueryProcessorOutput = {
	originalQuery: "Quelles sont les conditions pour recevoir le SFT ?",
	processedQuery: "Quelles sont les conditions pour recevoir le SFT ?",
	reformulatedQuery: null,
	queryForRetrieval: "Quelles sont les conditions pour recevoir le SFT ?",
	expandedAcronyms: ["SFT"],
	detectedAcronyms: { SFT: "supplément familial de traitement" },
	wasExpanded: true,
	isInScope: true,
	shouldProceed: true,
	intent: "rag_query",
	intentConfidence: 0.99,
	intentReason: "Question RH",
	needsLegalSearch: false,
	theme: "remuneration",
	isBetaExcludedTheme: false,
	requestedSource: null,
	isCatalogQuery: false,
	catalogKeyword: null,
	directResponse: null,
	intentRawResponse: null,
	providerUsed: "none",
	promptNameUsed: "test",
};

function makeChunk(id: string, publisher = "Service-Public"): RetrievedChunk {
	return {
		chunkId: id,
		text: `Chunk ${id}`,
		score: 0.9,
		tableSource: publisher,
		publisher,
		publisherKey: publisher.toLowerCase().replace(/[^a-z0-9]+/g, "_"),
		sectionId: `section-${id}`,
		metadata: { text: `Chunk ${id}`, publisher, section_id: `section-${id}` },
		embeddingModelUsed: "albert",
		retrievalMode: "semantic",
		sourceIndex: "rag_chunks_albert",
	};
}

function makeRetrieverOutput(chunk: RetrievedChunk, topK: number, mode: "semantic" | "hybrid") {
	return {
		chunks: [chunk],
		retrievalMeta: {
			configuredSearchMode: mode,
			embeddingModelConfigured: "albert",
			embeddingModelUsed: "albert",
			embeddingProviderUsed: "albert",
			indexName: "rag_chunks_albert",
			topKPerPublisher: topK,
			alpha: 0.5,
			publishersSearched: [chunk.publisherKey],
			modeByPublisher: { [chunk.publisherKey]: mode },
			warnings: [],
			chunkCount: 1,
			rankingStrategy: "global_rrf",
		},
	} satisfies RetrieverOutput;
}

function makeSection(chunk: RetrievedChunk): AggregatedSection {
	return {
		sectionId: chunk.sectionId,
		heading: `Heading ${chunk.chunkId}`,
		markdown: `Markdown ${chunk.chunkId}`,
		chunks: [chunk],
		score: chunk.score,
		documentId: `doc-${chunk.chunkId}`,
		publisher: chunk.publisher,
		referencesJuridiques: null,
		headingPath: `Heading ${chunk.chunkId}`,
		metadata: { doc_id: `doc-${chunk.chunkId}` },
	};
}

function makeAggregationOutput(section: AggregatedSection): SectionAggregatorOutput {
	return {
		sections: [section],
		aggregationMeta: {
			weights: { max: 0.5, mean: 0.3, count: 0.2 },
			sectionCountBeforeRerank: 1,
			sectionCountAfterRerank: 1,
			rerankerEnabled: false,
			rerankerApplied: false,
			rerankerTopK: 10,
			rerankerCandidateCount: 1,
			warnings: [],
		},
	};
}

function makeSelectorOutput(args: {
	sections: AggregatedSection[];
	allRejected: boolean;
	reason: string;
}): ContextSelectorOutput {
	return {
		sections: args.allRejected ? [] : args.sections,
		shortCircuit: args.allRejected,
		shortCircuitMessage: args.allRejected ? "Je n'ai pas trouvé de contexte utile." : null,
		selectorMeta: {
			enabled: true,
			providerConfigured: "albert",
			providerUsed: "none",
			modelUsed: "test-selector",
			promptNameUsed: "test-selector.md",
			selectedCount: args.allRejected ? 0 : args.sections.length,
			removedCount: args.allRejected ? args.sections.length : 0,
			kept: args.allRejected
				? []
				: args.sections.map((section, idx) => ({
						idx,
						heading: section.heading,
						publisher: section.publisher ?? "",
					})),
			removed: args.allRejected
				? args.sections.map((section, idx) => ({
						idx,
						heading: section.heading,
						publisher: section.publisher ?? "",
					}))
				: [],
			reason: args.reason,
			rawResponse: args.allRejected ? '{"selected_ids": []}' : '{"selected_ids": [0]}',
			allRejected: args.allRejected,
			fallbackMode: args.allRejected ? "explicit_all_rejected" : "none",
			warnings: [],
		},
	};
}

function makeContextBuilderOutput(section: AggregatedSection): ContextBuilderOutput {
	return {
		contextItems: [
			{
				sectionId: section.sectionId,
				heading: section.heading,
				content: section.markdown,
				score: section.score,
				publisher: section.publisher,
				documentTitle: section.heading,
				documentUrl: null,
				referencesJuridiques: null,
				tokenEstimate: 5,
				metadata: section.metadata,
			},
		],
		context: section.markdown,
		contextMeta: {
			contextMode: "standard",
			tokenBudget: 8000,
			tokenCount: 5,
			refsTokenCount: 0,
			maxSections: 12,
			selectedCount: 1,
			fullDocCount: 0,
			triangulationAdded: 0,
			legalRefsResolvedCount: 0,
			legalRefsInjectedCount: 0,
			lastResolvedRefs: {},
			warnings: [],
		},
	};
}

const generatorOutput: GeneratorOutput = {
	answer: "Le SFT est versé sous conditions.",
	generationMeta: {
		providerConfigured: "albert",
		modelConfigured: "test-generator",
		fallbackProviderConfigured: "scaleway",
		fallbackModelConfigured: "fallback",
		providerUsed: "albert",
		modelUsed: "test-generator",
		fallbackTriggered: false,
		promptNameUsed: "system.md",
		systemPromptUsed: "system",
		fullPrompt: "prompt",
		generationMs: 1,
		ttftMs: 1,
		charsPerSecond: 100,
		responseLengthTokens: 8,
		warnings: [],
	},
};

async function testRetryCanRecoverContext() {
	const initialChunk = makeChunk("initial", "MATTE");
	const retryChunk = makeChunk("retry", "Service-Public");
	const initialSection = makeSection(initialChunk);
	const retrySection = makeSection(retryChunk);
	const retrieverCalls: Array<Parameters<RagPipelineExecutionDependencies["runRetriever"]>[0]> = [];
	let contextBuilderCalls = 0;
	let generatorCalls = 0;

	const dependencies: Partial<RagPipelineExecutionDependencies> = {
		getRuntimeRagConfig: async () => DEFAULT_RUNTIME_RAG_CONFIG,
		runRetriever: async (input) => {
			retrieverCalls.push(input);
			return retrieverCalls.length === 1
				? makeRetrieverOutput(initialChunk, 15, "semantic")
				: makeRetrieverOutput(retryChunk, 30, "hybrid");
		},
		runSectionAggregator: async (input) => {
			const chunk = input.chunks[0];
			assert.ok(chunk);
			return makeAggregationOutput(chunk.chunkId === "initial" ? initialSection : retrySection);
		},
		runContextSelector: async (input) =>
			retrieverCalls.length === 1
				? makeSelectorOutput({
						sections: input.sections,
						allRejected: true,
						reason: "Aucune section pertinente.",
					})
				: makeSelectorOutput({
						sections: input.sections,
						allRejected: false,
						reason: "La recherche hybride trouve une section utile.",
					}),
		runContextBuilder: async (input) => {
			contextBuilderCalls += 1;
			const section = input.sections[0];
			assert.ok(section);
			return makeContextBuilderOutput(section);
		},
		runGenerator: async () => {
			generatorCalls += 1;
			return generatorOutput;
		},
	};

	const result = await runRagPipelineRagBranch({
		inputData,
		state: {},
		setState: async () => {},
		dependencies,
	});

	assert.equal(result.answer, generatorOutput.answer);
	assert.equal(retrieverCalls.length, 2);
	assert.equal(retrieverCalls[1]?.config?.search_mode, "hybrid");
	assert.equal(retrieverCalls[1]?.config?.initial_top_k, 30);
	assert.equal(contextBuilderCalls, 1);
	assert.equal(generatorCalls, 1);
	assert.equal(result.metadata.selector_retry_triggered, true);
	assert.equal(result.metadata.selector_retry_succeeded, true);
	assert.equal(result.metadata.selector_all_rejected, false);
	assert.equal(result.metadata.selected_retrieval_attempt, "selector_retry");
	assert.deepEqual(
		(
			result.metadata.retrieval_attempts as Array<{ name: string; selector_all_rejected: boolean }>
		).map((attempt) => [attempt.name, attempt.selector_all_rejected]),
		[
			["initial", true],
			["selector_retry", false],
		],
	);
}

async function testRetryPreservesNoAnswerWhenSecondAttemptRejectsAll() {
	const initialChunk = makeChunk("initial", "MATTE");
	const retryChunk = makeChunk("retry", "Service-Public");
	const initialSection = makeSection(initialChunk);
	const retrySection = makeSection(retryChunk);
	const retrieverCalls: Array<Parameters<RagPipelineExecutionDependencies["runRetriever"]>[0]> = [];

	const dependencies: Partial<RagPipelineExecutionDependencies> = {
		getRuntimeRagConfig: async () => DEFAULT_RUNTIME_RAG_CONFIG,
		runRetriever: async (input) => {
			retrieverCalls.push(input);
			return retrieverCalls.length === 1
				? makeRetrieverOutput(initialChunk, 15, "semantic")
				: makeRetrieverOutput(retryChunk, 30, "hybrid");
		},
		runSectionAggregator: async (input) => {
			const chunk = input.chunks[0];
			assert.ok(chunk);
			return makeAggregationOutput(chunk.chunkId === "initial" ? initialSection : retrySection);
		},
		runContextSelector: async (input) =>
			makeSelectorOutput({
				sections: input.sections,
				allRejected: true,
				reason: retrieverCalls.length === 1 ? "Aucune section pertinente." : "Toujours rien.",
			}),
		runContextBuilder: async () => {
			throw new Error("Context builder must not run when retry rejects all context.");
		},
		runGenerator: async () => {
			throw new Error("Generator must not run when retry rejects all context.");
		},
	};

	const result = await runRagPipelineRagBranch({
		inputData,
		state: {},
		setState: async () => {},
		dependencies,
	});

	assert.equal(result.shortCircuit, true);
	assert.equal(result.contextItems.length, 0);
	assert.equal(retrieverCalls.length, 2);
	assert.equal(result.metadata.selector_retry_triggered, true);
	assert.equal(result.metadata.selector_retry_succeeded, false);
	assert.equal(result.metadata.selector_all_rejected, true);
	assert.equal(result.metadata.selected_retrieval_attempt, "selector_retry");
}

await testRetryCanRecoverContext();
await testRetryPreservesNoAnswerWhenSecondAttemptRejectsAll();

console.log("selector retry smoke tests passed");
