---
name: evidence-audit
description: Adversarial provenance audit of a draft deliverable before it ships — re-derive load-bearing claims at their sources, downgrade what fails. Use before publishing any client- or stakeholder-facing artifact containing numbers, ratings, prices, or "verified" language; or when the operator says "audit this", "check the claims", or "is this actually verified?"
---

# Evidence Audit

Adversarial audit pass for outbound deliverables. The rule being enforced (working-style.md § Audit discipline): **an artifact's claim to have been verified is not verification** — and that applies to your own drafts most of all. Search-tool snippets are hearsay; a "verified" label you attached without fetching the source is self-attestation.

## Procedure

1. **Extract the load-bearing claims.** Scan the draft for: prices/fees, ratings and review counts, statistics ("1 in 3...", "N% of..."), superlatives tied to numbers, and every instance of "verified / confirmed / checked directly / pulled directly." Load-bearing = a claim whose failure would change the recommendation or embarrass the sender. List them with locations.

2. **Trace each to its actual provenance — from the session transcript, not from memory.** For each claim, answer: was the source page fetched in-session (WebFetch/browse-tool), or did this number arrive in a search-result summary? Who owns the domain it came from? Vendor self-comparisons and competitor "analyses" are marketing, not evidence — a competitor's stat about its rival is the same failure as the vendor's stat about itself.

3. **Re-derive what carries "verified" language.** Fetch the source directly. Three outcomes:
   - **Confirms** → keep the label, note the fetch.
   - **Contradicts** → fix the claim, not the label.
   - **Source blocks fetching** (G2, Trustpilot 403s) → the claim *cannot* be labeled verified by anyone; downgrade to "search-corroborated," say the source blocks direct reads, and check whether the recommendation survives without the number. If it doesn't, the recommendation needs different footing.

4. **Check the deliverable's own confidence taxonomy against itself.** If the doc promises "green = read at the source," every green tag must trace to a fetch in this session. A confidence system that lies on one row is worse than no confidence system.

5. **Hunt the cost/claim gaps.** What does the draft imply is free, complete, or settled that isn't priced or checked? (Origin case: a "$0 to start" claim while the plan's only real recurring cost — video hosting — went unpriced.)

6. **Report corrections, then apply them.** Lead with what changed and why; keep the deliverable's conclusions only if they survive on the downgraded evidence.

## Notes

- forge-signal has a deterministic version of steps 1–2 as a pipeline role: `src/pipeline/roles/fact-auditor.ts` (claim extraction + provenance ledger). This skill is the full pass including re-derivation, which needs live fetches.
- For client-facing artifact *design* provenance (palette/type grounded in the client's real brand), see `forge-signal/scripts/harvest-brand.mjs`.
- Do not fabricate a fetch you didn't make. "I could not verify X" in the report is a valid, useful finding.
