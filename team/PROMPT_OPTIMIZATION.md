# Prompt calibration

Baseline: 21ea650. The English prompt removes repeated instructions while retaining the JSON fields, Russian examples and literal buyer evidence. The catalog is still excluded; mandatory Gemma calls and history handling are unchanged.

Measured through the configured Gemma gateway, using usage.prompt_tokens (including the request messages):

| Request | Previous prompt | New prompt |
| --- | ---: | ---: |
| Нужен стол | 2322 | 1130 |

Both responses used 55 completion tokens and recognized table. Input savings on this paired sample: 51.3%. This is not a corpus-wide estimate or a proof of maximal compression. English translation and content compression were changed together; their individual effects were not measured.

Further live checks of the new prompt passed: 2 million rubles with table width <=100 cm in Taganrog; unsupported bicycle in Moscow (requested product/location metadata); grooming-table purpose; switching a three-item bundle to a grooming table with 15000 budget and no stale width. Two initial verification scripts had incorrect assertions about Python response types (Comparison is a dataclass; absent categories normalize to an empty tuple); these were harness issues, not failed gateway responses.

Local regression: 168 Python tests passed; 38 UI/dialogue/speech scenarios passed. These tests mostly mock model replies and do not establish semantic equivalence for all natural-language inputs. Live checks cover only the cases above.

Commands: `.venv/bin/python -m unittest discover -q`, `node tests/ui_dialogue.cjs`. Reproduce token comparison by loading SYSTEM from baseline `buyer/prompt.py` and current SYSTEM into `buyer.model.SYSTEM`, calling `extract` with the same user message and reading ModelTrace.prompt_tokens. Respect gateway pacing and never log credentials.
