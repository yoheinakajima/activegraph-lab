# Inconsistent relation encoding breaks view traversal in activegraph-packs

## What we tried

I set out to read the activegraph.ai codebase end-to-end, documenting every unevidenced claim as a research gap[^1]. During this audit, I examined how different components in the activegraph-packs repository handle relation encoding.

## What happened

I discovered a fundamental inconsistency in how relations are encoded across the codebase[^2]. The core, research, and tool_gateway components write the relation type into the `source` field, effectively treating it as `(type, target, source)`. Meanwhile, the chat component follows the documented signature order of `(source, target, type)`.

This split creates a critical problem: view traversal only follows relations encoded in signature order. Since the two encoding approaches are not equivalent, relations written by core/research/tool_gateway components become invisible to the traversal system.

The lab's current approach is to write relations in signature order and decode both formats, as documented in ADR-008[^2]. However, this doesn't solve the underlying inconsistency across the broader codebase.

## What it means

This finding reveals a significant architectural flaw that undermines the graph's connectivity. When different components encode relations differently, the resulting graph becomes fragmented. Relations that should connect concepts remain invisible to traversal algorithms, breaking the fundamental promise of a connected knowledge graph.

The impact extends beyond technical correctness. If view traversal can't follow certain relations, then queries, recommendations, and graph-based reasoning will miss critical connections. This could lead to incomplete results, failed lookups, and degraded system behavior that would be difficult to debug.

The fact that this inconsistency exists suggests insufficient coordination between component teams and possibly inadequate integration testing. It also indicates that the relation encoding specification may not be clearly documented or enforced across the codebase.

## What's next

I need to map the full extent of this inconsistency across all activegraph-packs components. This means auditing every `add_relation` call to determine which encoding each component uses and documenting the scope of affected functionality.

The next step is proposing a unified encoding standard and migration strategy. This will likely require coordinating with component maintainers to ensure backward compatibility during the transition.

I also need to investigate whether existing relations in the graph are affected by this split. If so, a data migration may be necessary to ensure all relations follow the same encoding standard.

Finally, I should examine the testing infrastructure to understand how this inconsistency went undetected. Adding integration tests that verify relation encoding consistency across components would prevent similar issues in the future.

[^1]: branch#2
[^2]: observation#5

> **Review note (claims coverage):** paragraph(s) 5, 8, 9, 10, 12, 13, 14, 15 carry no evidence footnotes. Verify or cut before approving.

---
*Provenance:* branch `branch#2` · evidence `observation#3`, `artifact#4`, `observation#5` · as of event `evt_019` · model `claude-sonnet-4-20250514` · crawl `synthetic`

*Note: this run crawled a synthetic snapshot, not the live site — treat site claims accordingly.*
