# ADR 0007: Code calculates every number; the LLM never does

- **Status:** accepted (S0, 2026-11-20)
- **Deciders:** FDE team; reviewed with Vikram (IT) and Meera (sponsor)

## Context
A quote's price, delivery date, CO₂ figure and validity are commitments with money attached. LLMs are good at reading and writing, but they make arithmetic and lookup errors and can be manipulated by injected text.

## Decision
Prices, dates, CO₂ and terms are computed only by **deterministic code** (`costing/`) from Odoo data, rates, policy and cited factors, and every line carries its **provenance**. The LLM extracts inputs (with source and confidence) and writes the reply text. A **faithfulness check** fails the draft if it contains any number not present in the computed breakdown.

## Consequences
+ Numbers are reproducible, testable (`hypothesis` property tests) and explainable to Ramesh.
+ An injection can't change a price by talking to the model.
− Extraction quality still limits accuracy; low-confidence fields go to a human.
