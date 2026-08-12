# Sources

External material that influenced implementation decisions. Nothing listed here was copied
into the repository; each entry records a specification or standard consulted for correct
behaviour.

## Standards and specifications

| Source | Where it influenced this repository |
|---|---|
| OpenAPI Specification 3.0.x and 3.1.x | Path-item versus operation parameter override semantics, request body and response object shapes, security requirement objects and security scheme types. `src/api_mcp_compiler/ingest/openapi.py`. |
| RFC 6901, JavaScript Object Notation (JSON) Pointer | Reference token escaping (`~0`, `~1`) and URI-fragment form for source pointers. `src/api_mcp_compiler/provenance.py`. |
| RFC 9110, HTTP Semantics | Method safety and idempotency, used as the basis for the proposed side-effect and idempotency classes. PATCH is deliberately left unclassified because RFC 9110 does not make it idempotent. `src/api_mcp_compiler/ingest/openapi.py`. |
| WSDL 1.1 (W3C Note, 15 March 2001) | Document structure: `portType`, `binding`, `service`/`port`, `message`/`part`, and the SOAP binding extension elements `soap:binding`, `soap:operation`, `soap:address`. `src/api_mcp_compiler/ingest/wsdl.py`. |
| WSDL 2.0 (W3C Recommendation) | Namespace identification only, so that a WSDL 2.0 document is rejected explicitly rather than parsed into an empty result. |
| XPath 1.0 | Location step and predicate syntax, and the absence of an escape sequence inside string literals, which is why `xpath_literal` falls back to `concat()`. |
| JSON Schema Draft 2020-12 | Contract schemas under `schemas/`. |
| Model Context Protocol specification | Target artifact vocabulary: tools, resources and prompts. Not yet integrated; the MCP Python SDK is deliberately not a dependency until the tool plan and policy models are stable. |

## Prior and related work

| Source | Relevance |
|---|---|
| Making REST APIs Agent-Ready: From OpenAPI to Model Context Protocol Servers for Tool-Augmented LLMs (arXiv:2507.16044) | AutoMCP compiles OpenAPI 2.0 and 3.0 into executable MCP servers by mapping each endpoint to exactly one tool, injecting existing authentication metadata, and generating runtime scaffolding. It performs no semantic grouping, composition or schema simplification, and synthesises no policy. It is therefore a published implementation of what this project calls the baseline, and is the correct thing to compare against rather than an invented straw man. Its reported finding that the bottleneck moves from code generation to specification quality is the same observation that motivated ambiguity records and the completeness sweep here. |
| RestGPT and RestBench (arXiv:2306.06624) | A benchmark of realistic user instructions with human-annotated solution paths over two real services, TMDB and Spotify, distributed with OpenAPI specifications. Relevant as a third-party source of both specifications and task goals, which self-authored fixtures cannot provide. |
| tau-bench | Evaluates tool-using agents by comparing final database state rather than tool-call resemblance. Independent support for the final-state oracle design used here. |
| AppWorld | A stateful benchmark over 457 APIs across nine simulated applications, evaluated on outcome rather than trajectory. |
| MCPVerse, MCP-Bench, MCPToolBench++ | Benchmarks that evaluate agents against already-built MCP servers. They measure model behaviour given a tool surface, not the design of the surface itself, so they are related work rather than a usable input here. |

## Security references

| Source | Where it influenced this repository |
|---|---|
| OWASP XML External Entity (XXE) Prevention Cheat Sheet | Hardened `lxml` parser configuration for ingesting third-party WSDL documents. `src/api_mcp_compiler/ingest/wsdl.py`. |

## Code fragments

None. No third-party code has been copied into this repository.

## Datasets

None. All example specifications and evaluation tasks are synthetic and authored for this
project.
