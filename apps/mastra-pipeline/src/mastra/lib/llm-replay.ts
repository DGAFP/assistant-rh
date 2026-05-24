import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

export type LlmReplayMode = "off" | "replay" | "record";

export interface LlmReplayRequest {
	stage: string;
	payload: unknown;
}

export interface LlmReplayEntry {
	key: string;
	stage: string;
	requestDigest: string;
	response: string;
	createdAt: string;
	metadata: Record<string, unknown> | null;
}

interface LlmReplayCacheFile {
	version: 1;
	entries: LlmReplayEntry[];
}

function compareStrings(left: string, right: string): number {
	if (left < right) {
		return -1;
	}
	if (left > right) {
		return 1;
	}
	return 0;
}

function normalizeForStableJson(value: unknown): unknown {
	if (Array.isArray(value)) {
		return value.map((item) => normalizeForStableJson(item));
	}

	if (value instanceof Date) {
		return value.toISOString();
	}

	if (value && typeof value === "object") {
		const entries = Object.entries(value as Record<string, unknown>).sort(([left], [right]) =>
			compareStrings(left, right),
		);

		const normalized: Record<string, unknown> = {};
		for (const [key, nested] of entries) {
			normalized[key] = normalizeForStableJson(nested);
		}

		return normalized;
	}

	return value;
}

function stableJson(value: unknown): string {
	return JSON.stringify(normalizeForStableJson(value));
}

function hashSha256(value: string): string {
	return createHash("sha256").update(value).digest("hex");
}

function parseCacheFile(path: string): LlmReplayCacheFile {
	const raw = JSON.parse(readFileSync(path, "utf-8")) as unknown;

	if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
		throw new Error(`Replay cache at ${path} is not a JSON object`);
	}

	const version = (raw as Record<string, unknown>).version;
	if (version !== 1) {
		throw new Error(`Unsupported replay cache version in ${path}: ${String(version)}`);
	}

	const entries = (raw as Record<string, unknown>).entries;
	if (!Array.isArray(entries)) {
		throw new Error(`Replay cache in ${path} has invalid 'entries' (expected array)`);
	}

	const parsedEntries: LlmReplayEntry[] = [];
	for (const entry of entries) {
		if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
			continue;
		}

		const candidate = entry as Record<string, unknown>;
		if (
			typeof candidate.key !== "string" ||
			typeof candidate.stage !== "string" ||
			typeof candidate.requestDigest !== "string" ||
			typeof candidate.response !== "string" ||
			typeof candidate.createdAt !== "string"
		) {
			continue;
		}

		parsedEntries.push({
			key: candidate.key,
			stage: candidate.stage,
			requestDigest: candidate.requestDigest,
			response: candidate.response,
			createdAt: candidate.createdAt,
			metadata:
				candidate.metadata &&
				typeof candidate.metadata === "object" &&
				!Array.isArray(candidate.metadata)
					? (candidate.metadata as Record<string, unknown>)
					: null,
		});
	}

	return {
		version: 1,
		entries: parsedEntries,
	};
}

export function buildLlmReplayKey(request: LlmReplayRequest): {
	key: string;
	requestDigest: string;
} {
	const requestDigest = hashSha256(stableJson(request.payload));
	return {
		key: `${request.stage}:${requestDigest}`,
		requestDigest,
	};
}

export class LlmReplayStore {
	private readonly path: string;
	private readonly entriesByKey = new Map<string, LlmReplayEntry>();
	private dirty = false;

	constructor(path: string) {
		this.path = path;

		if (!existsSync(this.path)) {
			return;
		}

		const cacheFile = parseCacheFile(this.path);
		for (const entry of cacheFile.entries) {
			this.entriesByKey.set(entry.key, entry);
		}
	}

	get entryCount(): number {
		return this.entriesByKey.size;
	}

	get(request: LlmReplayRequest): LlmReplayEntry | null {
		const key = buildLlmReplayKey(request).key;
		return this.entriesByKey.get(key) ?? null;
	}

	upsert(
		request: LlmReplayRequest,
		response: string,
		metadata: Record<string, unknown> | null = null,
	): LlmReplayEntry {
		const { key, requestDigest } = buildLlmReplayKey(request);
		const entry: LlmReplayEntry = {
			key,
			stage: request.stage,
			requestDigest,
			response,
			createdAt: new Date().toISOString(),
			metadata,
		};

		this.entriesByKey.set(key, entry);
		this.dirty = true;
		return entry;
	}

	saveIfDirty(): void {
		if (!this.dirty) {
			return;
		}

		mkdirSync(dirname(this.path), { recursive: true });

		const payload: LlmReplayCacheFile = {
			version: 1,
			entries: Array.from(this.entriesByKey.values()).sort((left, right) =>
				compareStrings(left.key, right.key),
			),
		};

		writeFileSync(this.path, `${JSON.stringify(payload, null, 2)}\n`, "utf-8");
		this.dirty = false;
	}
}
