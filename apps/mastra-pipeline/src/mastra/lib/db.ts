import { Pool, type PoolConfig } from "pg";

const DSN_ENV_KEYS = ["SCW_POSTGRES_DSN"] as const;
let pool: Pool | null = null;

function parseInteger(raw: string | undefined, fallback: number): number {
	if (!raw) {
		return fallback;
	}

	const parsed = Number.parseInt(raw, 10);
	return Number.isFinite(parsed) ? parsed : fallback;
}

function normalizeJsonObject(value: unknown): Record<string, unknown> {
	if (value && typeof value === "object" && !Array.isArray(value)) {
		return value as Record<string, unknown>;
	}

	if (typeof value === "string") {
		try {
			const parsed = JSON.parse(value) as unknown;
			if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
				return parsed as Record<string, unknown>;
			}
		} catch {
			// Ignore parse error and fallback to empty object.
		}
	}

	return {};
}

export function resolveDatabaseUrl(env: NodeJS.ProcessEnv = process.env): string {
	const target = env.APP_DB_TARGET?.trim().toLowerCase();
	if (target) {
		if (target !== "scaleway") {
			throw new Error(`Unsupported APP_DB_TARGET=${JSON.stringify(target)} (expected: scaleway).`);
		}

		const value = env.SCW_POSTGRES_DSN?.trim();
		if (value) {
			return value;
		}

		throw new Error("APP_DB_TARGET=scaleway requires SCW_POSTGRES_DSN to be set.");
	}

	for (const key of DSN_ENV_KEYS) {
		const value = env[key]?.trim();
		if (value) {
			return value;
		}
	}

	throw new Error(`Missing PostgreSQL DSN. Set one of: ${DSN_ENV_KEYS.join(", ")}`);
}

function createPoolConfig(connectionString: string): PoolConfig {
	return {
		connectionString,
		max: parseInteger(process.env.DB_POOL_MAX, 10),
		idleTimeoutMillis: parseInteger(process.env.DB_IDLE_TIMEOUT_MS, 30_000),
		connectionTimeoutMillis: parseInteger(process.env.DB_CONNECT_TIMEOUT_MS, 10_000),
	};
}

export function getDbPool(): Pool {
	if (pool) {
		return pool;
	}

	const connectionString = resolveDatabaseUrl();
	pool = new Pool(createPoolConfig(connectionString));
	return pool;
}

export async function closeDbPool(): Promise<void> {
	if (!pool) {
		return;
	}

	await pool.end();
	pool = null;
}

export async function pingDb(): Promise<boolean> {
	const db = getDbPool();
	await db.query("SELECT 1");
	return true;
}

export async function loadRuntimeConfig(id = 1): Promise<Record<string, unknown>> {
	const db = getDbPool();
	const result = await db.query<{ config: unknown }>(
		"SELECT config FROM rag_config WHERE id = $1",
		[id],
	);

	if (result.rowCount === 0) {
		return {};
	}

	return normalizeJsonObject(result.rows[0]?.config);
}

export async function loadSystemPrompt(name: string): Promise<string | null> {
	const db = getDbPool();
	const result = await db.query<{ content: string }>(
		"SELECT content FROM system_prompts WHERE name = $1 AND is_active = TRUE LIMIT 1",
		[name],
	);

	if (result.rowCount === 0) {
		return null;
	}

	const content = result.rows[0]?.content;
	return typeof content === "string" ? content : null;
}

export async function loadAcronymMap(): Promise<Record<string, string>> {
	const db = getDbPool();
	const result = await db.query<{ acronym: string; expansion: string }>(
		"SELECT acronym, expansion FROM acronyms ORDER BY priority DESC",
	);

	const acronyms: Record<string, string> = {};
	for (const row of result.rows) {
		if (row.acronym && row.expansion) {
			acronyms[row.acronym] = row.expansion;
		}
	}

	return acronyms;
}

export interface DbHealth {
	ok: boolean;
	dsnSource: (typeof DSN_ENV_KEYS)[number] | null;
}

export function getDsnSource(
	env: NodeJS.ProcessEnv = process.env,
): (typeof DSN_ENV_KEYS)[number] | null {
	for (const key of DSN_ENV_KEYS) {
		if (env[key]) {
			return key;
		}
	}

	return null;
}

export async function getDbHealth(): Promise<DbHealth> {
	try {
		await pingDb();
		return { ok: true, dsnSource: getDsnSource() };
	} catch {
		return { ok: false, dsnSource: getDsnSource() };
	}
}
