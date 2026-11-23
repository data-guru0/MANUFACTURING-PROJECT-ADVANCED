# Model cards (S9)

Every model the agents use is an **alias** in `agent/config/models.yaml`, reached only through LiteLLM, versioned in
Langfuse as `apex-models` (promoted by a person with an evaluation, `agent/ops/prompts.py`). Verified to exist on Groq on
2026-11-20 (S1). Inference is external (Groq, open-weight models); embeddings and the cycle-time model run locally.

| Alias | Model | Used by (agent) | Purpose | Inputs it sees | Known limits | Evaluation |
|---|---|---|---|---|---|---|
| `quote-guard` | Meta Llama Prompt Guard 2 (86M) | security_gate | Prompt-injection score per text chunk | Email body, hidden text, PDF and DXF text (masked) | English-centred; a score, not a reason; ≤ 20 chunks per email | `TRIAGE_EVAL.md`: 3/3 injections flagged, 0 false alarms on 65; red team 1-3 |
| `quote-triage` | OpenAI gpt-oss-20b | triage | Classify an email (RFQ, follow-up, supplier, not an RFQ) | The email in an untrusted block | Free-tier TPM limits throughput | `TRIAGE_EVAL.md`: 68/68 |
| `quote-reasoner` | OpenAI gpt-oss-120b | extractor, drafter | Read the email's requirements; write the reply around computed numbers | Email text (untrusted block); computed facts | ≈200K tokens/day free tier; may refuse unusual requests (seen in S9) | `EXTRACTION_EVAL.md` (requirements 100 %); faithfulness check on every draft |
| `quote-reasoner-lite` | OpenAI gpt-oss-20b | extractor, drafter (fallback) | Same, when the big model is unavailable | Same | Lower quality: drafts still pass the faithfulness check or are flagged | Same checks |
| `quote-vision` | Qwen 3.8 27B (preview) | extractor | Transcribe scanned drawings (never interpret prices) | Up to 3 page images | Preview model; ≈40 scans/day free tier; sizes from scans always go to a person | `EXTRACTION_EVAL.md`: scans size → person; NIST results |
| `quote-safety` | OpenAI gpt-oss-safeguard-20b | gateway guardrail | Judge every drafted reply against Apex's outbound policy | The drafted reply only | Policy-following classifier; "no clear SAFE" blocks | `test_every_drafted_reply_is_judged_and_the_judge_knows_unsafe_from_safe` |
| (local) BAAI/bge-small-en-v1.5 | fastembed, 384 dims | knowledge | Embeddings for analog search | Part descriptions | English | `RETRIEVAL_EVAL.md`: 97.5 % top-5 |
| (local) cycle-time model | scikit-learn HistGradientBoosting, P10/P50/P90 | costing (code) | Actual vs standard minutes per work order | Finished work orders (Odoo) | 228 work orders; promoted only if better | Card in MinIO `models/cycle_time/<version>/card.json`; `COSTING_EVAL.md` |

**What no model does:** compute a price, a date, a CO₂ figure or a term (code does: ADR 0007); decide a permission (OPA does); send anything (a person approves, the write tools check the signature).
