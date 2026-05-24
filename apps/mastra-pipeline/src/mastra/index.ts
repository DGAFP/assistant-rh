import { Mastra } from "@mastra/core";
import { testAgent } from "./agents/index";
import { AlbertAPIGateway } from "./gateways/albert";
import { chatCompletionsRoute, modelsRoute } from "./routes";
import { contextBuilderWorkflow } from "./workflows/context-builder";
import { queryProcessorWorkflow } from "./workflows/query-processor";
import { ragPipelineWorkflow } from "./workflows/rag-pipeline";
import { retrieverWorkflow } from "./workflows/retriever";
import { sectionAggregatorWorkflow } from "./workflows/section-aggregator";

export const mastra = new Mastra({
	agents: { testAgent },
	workflows: {
		queryProcessorWorkflow,
		retrieverWorkflow,
		sectionAggregatorWorkflow,
		contextBuilderWorkflow,
		ragPipelineWorkflow,
	},
	gateways: {
		dinum: new AlbertAPIGateway(),
	},
	server: {
		apiRoutes: [chatCompletionsRoute, modelsRoute],
	},
});
