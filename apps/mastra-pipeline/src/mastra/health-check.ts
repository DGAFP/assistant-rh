import { fileURLToPath } from "node:url";
import { config as loadDotenv } from "dotenv";
import { getAlbertBaseUrl, getScalewayBaseUrl } from "./lib/albert";
import { closeDbPool, getDbHealth, getDsnSource, loadRuntimeConfig } from "./lib/db";

const appEnvPath = fileURLToPath(new URL("../../.env", import.meta.url));
const workspaceEnvPath = fileURLToPath(new URL("../../../.env", import.meta.url));

loadDotenv({ path: workspaceEnvPath, quiet: true });
loadDotenv({ path: appEnvPath, override: true, quiet: true });

interface EndpointStatus {
	enabled: boolean;
	ok: boolean;
	status: number | null;
	error: string | null;
}

async function checkModelsEndpoint(
	baseUrl: string,
	apiKey: string | undefined,
): Promise<EndpointStatus> {
	if (!apiKey) {
		return {
			enabled: false,
			ok: false,
			status: null,
			error: "API key is not set",
		};
	}

	try {
		const response = await fetch(`${baseUrl.replace(/\/$/, "")}/models`, {
			method: "GET",
			headers: {
				Authorization: `Bearer ${apiKey}`,
			},
			signal: AbortSignal.timeout(10_000),
		});

		return {
			enabled: true,
			ok: response.ok,
			status: response.status,
			error: response.ok ? null : `HTTP ${response.status}`,
		};
	} catch (error) {
		return {
			enabled: true,
			ok: false,
			status: null,
			error: error instanceof Error ? error.message : String(error),
		};
	}
}

async function main(): Promise<void> {
	try {
		const dsnSource = getDsnSource();

		const dbHealth = await getDbHealth();

		let runtimeConfigLoaded = false;
		try {
			await loadRuntimeConfig(1);
			runtimeConfigLoaded = true;
		} catch {
			runtimeConfigLoaded = false;
		}

		const albert = await checkModelsEndpoint(getAlbertBaseUrl(), process.env.ALBERT_API_KEY);

		const scaleway = await checkModelsEndpoint(getScalewayBaseUrl(), process.env.SCALEWAY_API_KEY);

		const strictMode = process.env.HEALTHCHECK_STRICT === "1";

		const report = {
			timestamp: new Date().toISOString(),
			strictMode,
			database: {
				dsnSource,
				ping: dbHealth.ok,
				ragConfigRead: runtimeConfigLoaded,
			},
			providers: {
				albert: {
					baseUrl: getAlbertBaseUrl(),
					...albert,
				},
				scaleway: {
					baseUrl: getScalewayBaseUrl(),
					...scaleway,
				},
			},
			promptSourceMode: process.env.PROMPT_SOURCE_MODE ?? "db_first",
		};

		console.log(JSON.stringify(report, null, 2));

		const hasDsn = dsnSource !== null;
		const dbOk = dbHealth.ok && runtimeConfigLoaded;
		const albertOk = albert.ok;

		const shouldFail = strictMode && (!hasDsn || !dbOk || !albertOk);
		if (shouldFail) {
			process.exitCode = 1;
		}
	} finally {
		await closeDbPool();
	}
}

void main();
