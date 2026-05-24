import { Agent } from "@mastra/core/agent";

/**
 * Minimal test agent to verify the Albert API gateway works in Mastra Studio.
 * Replace with real RAG pipeline agents in subsequent PRs.
 */
export const testAgent = new Agent({
	id: "test-agent",
	name: "Test Agent",
	instructions:
		"Tu es un assistant utile du Ministère de la Transition Écologique. " +
		"Tu réponds en français de manière concise et précise.",
	model: "dinum/albert/openweight-medium",
});
