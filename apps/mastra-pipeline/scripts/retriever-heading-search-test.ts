import assert from "node:assert/strict";
import { __retrieverTestHooks } from "../src/mastra/steps/retriever";

const { headingMatchScore, mergeCrossPublisherRanks } = __retrieverTestHooks;

function chunk(args: {
	chunkId: string;
	sectionId: string;
	score?: number;
	metadata?: Record<string, unknown>;
}) {
	return {
		chunkId: args.chunkId,
		text: `Chunk ${args.chunkId}`,
		score: args.score ?? 0.9,
		metadata: {
			publisher: "service_public",
			section_id: args.sectionId,
			text: `Chunk ${args.chunkId}`,
			...args.metadata,
		},
	};
}

function runHeadingScoreTests() {
	const exact = headingMatchScore(
		"Supplément familial de traitement",
		"Rémunération > Supplément familial de traitement",
		"Quelles sont les conditions pour recevoir le SFT ? supplément familial de traitement",
	);
	const near = headingMatchScore(
		"Supplément familial de traitement (SFT) dans la fonction publique",
		"Rémunération",
		"Qui peut bénéficier du SFT dans la fonction publique ?",
	);
	const unrelated = headingMatchScore(
		"Congé parental",
		"Temps de travail > Congés",
		"Quelles sont les conditions pour recevoir le SFT ?",
	);

	assert.equal(exact, 1);
	assert.ok(near > 0.5, `Expected near SFT heading match, got ${near}`);
	assert.equal(unrelated, 0);
}

function runHeadingMetadataFusionTests() {
	const headingOnly = chunk({
		chunkId: "heading-chunk",
		sectionId: "section-sft",
		score: 1,
		metadata: {
			retrieval_path: "heading",
			heading_search: true,
			heading_match_score: 1,
			matched_heading: "Supplément familial de traitement",
			matched_heading_path: "Rémunération > Supplément familial de traitement",
		},
	});

	const semanticSameSection = chunk({
		chunkId: "semantic-chunk",
		sectionId: "section-sft",
		score: 0.8,
		metadata: { retrieval_path: "chunk" },
	});

	const merged = mergeCrossPublisherRanks([
		{
			publisherKey: "service_public",
			mode: "semantic",
			sourcePath: "chunk",
			sourceName: "chunk:service_public",
			rows: [semanticSameSection],
		},
		{
			publisherKey: "service_public",
			mode: "lexical",
			sourcePath: "heading",
			sourceName: "heading:service_public",
			rows: [headingOnly],
		},
	]);

	const semantic = merged.find((row) => row.chunkId === "semantic-chunk");
	assert.ok(semantic, "Expected semantic row to remain in fused ranking");
	assert.equal(semantic.metadata.retrieval_path, "chunk+heading");
	assert.equal(semantic.metadata.heading_search, true);
	assert.equal(semantic.metadata.heading_match_score, 1);
	assert.equal(semantic.metadata.matched_heading, "Supplément familial de traitement");

	const heading = merged.find((row) => row.chunkId === "heading-chunk");
	assert.ok(heading, "Expected heading row to remain in fused ranking");
	assert.equal(heading.metadata.retrieval_path, "chunk+heading");
	assert.equal(heading.metadata.heading_search, true);
	assert.equal(heading.metadata.score_source, "heading:service_public");
}

runHeadingScoreTests();
runHeadingMetadataFusionTests();
console.log("Retriever heading search tests passed");
