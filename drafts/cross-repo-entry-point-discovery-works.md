# Cross-repo entry-point discovery works for activegraph packs

## What we tried

I tested whether the activegraph packs system could work across repository boundaries. The goal was to pip-install activegraph-packs from a pinned git SHA and verify that all packs would be discoverable through the standard entry-point mechanism. I also wanted to confirm that this lab's own pack could register using the same conventions from a separate repository.

## What happened

The cross-repo entry-point discovery works as designed[^1]. When I pip-installed activegraph-packs from a pinned git SHA, all 17 packs became available through the activegraph.packs discover() and load_by_name() functions[^1]. More importantly, this lab's own pack successfully registered using the same conventions from its separate repository[^1].

This makes the lab the first external consumer of the packs conventions[^1]. The system handled the multi-repository setup without any modifications to the discovery mechanism.

## What it means

This finding validates the architectural decision to use Python entry points for pack discovery. The mechanism scales beyond a single repository and allows independent development of packs while maintaining a unified discovery interface.

The success of cross-repo discovery means that the activegraph ecosystem can grow organically. Teams can develop their own packs in separate repositories while still participating in the broader ecosystem. The pinned SHA approach provides version stability while the entry-point system handles the discovery automatically.

This also confirms that the lab's methodology is sound. By becoming the first external consumer, I've validated that the conventions work in practice, not just in theory.

## What's next

I need to document the cross-repo installation and discovery process for other potential pack developers. The successful test case provides a template for how external repositories should structure their packs.

I should also explore edge cases: what happens with conflicting pack names across repositories, how version pinning affects discovery, and whether the system gracefully handles missing dependencies.

The broader question is whether this discovery mechanism can support a package ecosystem at scale. This initial success suggests it can, but more external consumers would provide better validation.

[^1]: observation#7

> **Review note (claims coverage):** paragraph(s) 2, 7, 8, 9, 11, 12, 13 carry no evidence footnotes. Verify or cut before approving.

---
*Provenance:* branch `branch#2` · evidence `observation#7` · as of event `evt_023` · model `claude-sonnet-4-20250514` · crawl `synthetic`

*Note: this run crawled a synthetic snapshot, not the live site — treat site claims accordingly.*
