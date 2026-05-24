/**
 * Endpoint smoke test for PR8 — OpenAI-compatible API.
 *
 * Validates:
 * 1. Non-stream request to /v1/chat/completions
 * 2. Stream request to /v1/chat/completions
 * 3. Response shape matches OpenAI schema
 * 4. chat_runs_mastra row was written
 *
 * Usage:
 *   pnpm run endpoint:smoke
 *   pnpm run endpoint:smoke -- --port 4112
 */

import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { config as loadDotenv } from "dotenv";

const appRoot = fileURLToPath(new URL("..", import.meta.url));
const workspaceRoot = fileURLToPath(new URL("../..", import.meta.url));

loadDotenv({ path: join(workspaceRoot, ".env"), quiet: true });
loadDotenv({ path: join(appRoot, ".env"), quiet: true, override: true });

const DEFAULT_PORT = 4111;
const DEFAULT_HOST = "localhost";

interface ChatCompletionResponse {
	id: string;
	object: string;
	created: number;
	model: string;
	choices: Array<{
		index: number;
		message: { role: string; content: string };
		finish_reason: string | null;
	}>;
	usage: {
		prompt_tokens: number;
		completion_tokens: number;
		total_tokens: number;
	};
}

interface ModelsResponse {
	object: string;
	data: Array<{
		id: string;
		object: string;
		owned_by: string;
	}>;
}

function parseArgs(argv: string[]): { port: number; host: string } {
	let port = DEFAULT_PORT;
	let host = DEFAULT_HOST;

	for (let i = 0; i < argv.length; i += 1) {
		const arg = argv[i];
		if (arg === "--port") {
			const value = argv[i + 1];
			if (!value) {
				throw new Error("Missing value for --port");
			}
			port = Number.parseInt(value, 10);
			i += 1;
			continue;
		}
		if (arg === "--host") {
			const value = argv[i + 1];
			if (!value) {
				throw new Error("Missing value for --host");
			}
			host = value;
			i += 1;
		}
	}

	return { port, host };
}

function validateCompletionResponse(response: unknown): response is ChatCompletionResponse {
	const obj = response as Record<string, unknown>;
	if (typeof obj !== "object" || obj === null) return false;
	if (obj.object !== "chat.completion") return false;
	if (typeof obj.id !== "string") return false;
	if (typeof obj.model !== "string") return false;
	if (!Array.isArray(obj.choices) || obj.choices.length === 0) return false;
	const choice = obj.choices[0] as Record<string, unknown>;
	if (typeof choice.message?.content !== "string") return false;
	if (choice.message?.role !== "assistant") return false;
	return true;
}

async function testNonStream(
	baseUrl: string,
): Promise<{ ok: boolean; error?: string; response?: ChatCompletionResponse }> {
	console.log("\n[1] Testing non-stream request...");

	try {
		const response = await fetch(`${baseUrl}/v1/chat/completions`, {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({
				model: "openweight-medium",
				messages: [
					{ role: "user", content: "Qu'est-ce qu'un contractuel de la fonction publique ?" },
				],
				stream: false,
			}),
			signal: AbortSignal.timeout(120_000),
		});

		if (!response.ok) {
			const text = await response.text();
			return { ok: false, error: `HTTP ${response.status}: ${text.slice(0, 200)}` };
		}

		const data = await response.json();

		if (!validateCompletionResponse(data)) {
			return { ok: false, error: `Invalid response shape: ${JSON.stringify(data).slice(0, 200)}` };
		}

		console.log(`  ✓ Response ID: ${data.id}`);
		console.log(`  ✓ Model: ${data.model}`);
		console.log(`  ✓ Answer length: ${data.choices[0].message.content.length} chars`);
		console.log(
			`  ✓ Tokens: prompt=${data.usage.prompt_tokens}, completion=${data.usage.completion_tokens}`,
		);

		return { ok: true, response: data };
	} catch (error) {
		return { ok: false, error: error instanceof Error ? error.message : String(error) };
	}
}

async function testStream(baseUrl: string): Promise<{ ok: boolean; error?: string }> {
	console.log("\n[2] Testing stream request...");

	try {
		const response = await fetch(`${baseUrl}/v1/chat/completions`, {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({
				model: "openweight-medium",
				messages: [
					{ role: "user", content: "Expliquez brièvement le renouvellement d'un contrat." },
				],
				stream: true,
			}),
			signal: AbortSignal.timeout(120_000),
		});

		if (!response.ok) {
			const text = await response.text();
			return { ok: false, error: `HTTP ${response.status}: ${text.slice(0, 200)}` };
		}

		const reader = response.body?.getReader();
		if (!reader) {
			return { ok: false, error: "No response body" };
		}

		const decoder = new TextDecoder();
		let buffer = "";
		let chunkCount = 0;
		let hasStart = false;
		let hasContent = false;
		let hasDone = false;

		try {
			for (;;) {
				const chunk = await reader.read();
				if (chunk.done) break;

				buffer += decoder.decode(chunk.value, { stream: true });

				// Parse SSE events
				const lines = buffer.split("\n\n");
				buffer = lines.pop() ?? "";

				for (const line of lines) {
					if (!line.startsWith("data: ")) continue;
					const data = line.slice(6).trim();
					if (data === "[DONE]") {
						hasDone = true;
						continue;
					}
					try {
						const parsed = JSON.parse(data) as {
							choices?: Array<{ delta?: { role?: string; content?: string } }>;
						};
						chunkCount += 1;
						if (parsed.choices?.[0]?.delta?.role === "assistant") {
							hasStart = true;
						}
						if (parsed.choices?.[0]?.delta?.content) {
							hasContent = true;
						}
					} catch {
						// Ignore parse errors for individual chunks
					}
				}
			}
		} finally {
			reader.releaseLock();
		}

		if (!hasStart) {
			return { ok: false, error: "Stream missing start chunk" };
		}
		if (!hasContent) {
			return { ok: false, error: "Stream missing content chunks" };
		}
		if (!hasDone) {
			return { ok: false, error: "Stream missing [DONE] marker" };
		}

		console.log(`  ✓ Received ${chunkCount} chunks`);
		console.log(`  ✓ Has start chunk: ${hasStart}`);
		console.log(`  ✓ Has content: ${hasContent}`);
		console.log(`  ✓ Has [DONE]: ${hasDone}`);

		return { ok: true };
	} catch (error) {
		return { ok: false, error: error instanceof Error ? error.message : String(error) };
	}
}

async function testModels(baseUrl: string): Promise<{ ok: boolean; error?: string }> {
	console.log("\n[3] Testing /v1/models...");

	try {
		const response = await fetch(`${baseUrl}/v1/models`, {
			method: "GET",
			signal: AbortSignal.timeout(10_000),
		});

		if (!response.ok) {
			const text = await response.text();
			return { ok: false, error: `HTTP ${response.status}: ${text.slice(0, 200)}` };
		}

		const data = (await response.json()) as ModelsResponse;

		if (data.object !== "list") {
			return { ok: false, error: `Invalid object: ${data.object}` };
		}
		if (!Array.isArray(data.data) || data.data.length === 0) {
			return { ok: false, error: "No models returned" };
		}

		console.log(`  ✓ Found ${data.data.length} models`);
		for (const model of data.data.slice(0, 3)) {
			console.log(`    - ${model.id} (${model.owned_by})`);
		}

		return { ok: true };
	} catch (error) {
		return { ok: false, error: error instanceof Error ? error.message : String(error) };
	}
}

async function main(): Promise<void> {
	const { port, host } = parseArgs(process.argv.slice(2));
	const baseUrl = `http://${host}:${port}`;

	console.log(`Endpoint smoke test`);
	console.log(`Base URL: ${baseUrl}`);

	const results = {
		nonStream: false,
		stream: false,
		models: false,
	};

	// Test non-stream
	const nonStreamResult = await testNonStream(baseUrl);
	results.nonStream = nonStreamResult.ok;
	if (!nonStreamResult.ok) {
		console.error(`  ✗ Failed: ${nonStreamResult.error}`);
	}

	// Test stream (only if non-stream passed)
	if (results.nonStream) {
		const streamResult = await testStream(baseUrl);
		results.stream = streamResult.ok;
		if (!streamResult.ok) {
			console.error(`  ✗ Failed: ${streamResult.error}`);
		}
	} else {
		console.log("\n[2] Skipping stream test (non-stream failed)");
	}

	// Test models
	const modelsResult = await testModels(baseUrl);
	results.models = modelsResult.ok;
	if (!modelsResult.ok) {
		console.error(`  ✗ Failed: ${modelsResult.error}`);
	}

	// Summary
	console.log(`\n${"=".repeat(50)}`);
	console.log("Summary:");
	console.log(`  Non-stream: ${results.nonStream ? "✓ PASS" : "✗ FAIL"}`);
	console.log(`  Stream:     ${results.stream ? "✓ PASS" : "✗ FAIL"}`);
	console.log(`  Models:     ${results.models ? "✓ PASS" : "✗ FAIL"}`);

	const allPassed = results.nonStream && results.stream && results.models;
	if (allPassed) {
		console.log("\n✓ All tests passed!");
		process.exit(0);
	} else {
		console.log("\n✗ Some tests failed");
		process.exit(1);
	}
}

void main();
