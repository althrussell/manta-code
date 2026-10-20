# Evaluation costs

`python -m evals` compares deterministic canned answers without calling a model.
Prices come from Manta's exact service pricing rules. An unknown service price
stays unknown, including in suite totals, savings, and win verdicts. The report
shows any known subtotal and how many tasks have prices.

A fully priced comparison exits successfully only when the candidate holds
quality and costs no more. An unpriced comparison runs as a quality-only demo:
it exits successfully when quality holds and explicitly reports that there is
no cost or savings verdict. Unit tests inject synthetic prices to qualify the
comparison arithmetic without inventing rates for current services.

`python -m evals --live` makes paid model requests through Manta. It reads tokens
and local cost estimates from rows tagged for that exact task, excluding other
concurrent runs. Missing records, missing prices, or incomplete usage leave cost
unknown. Local estimates do not establish Databricks billing charges.
