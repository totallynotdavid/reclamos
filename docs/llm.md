# Model calls

The offline [cards](cards.md) are the only code that calls a language model. No
route calls one, so a visitor never spends model credit. The code is in
`apps/api/src/reclamos_api/llm/`.

| File        | Owns                                                                |
| ----------- | ------------------------------------------------------------------- |
| `client.py` | The `LanguageModel` protocol, the Anthropic client and model prices |
| `spend.py`  | The cost log `llm_calls` and the limits checked before every call   |
| `quotes.py` | The rule that a quote is in its text                                |

## Quotes

A quote is in a text when it is at least 20 characters and appears in the text.
The comparison ignores case, accents, runs of whitespace and quote marks,
because models rewrite those. Any other difference fails the quote.

## Spend limits

Every model call goes through `Ledger.complete` in `spend.py`. It checks these
limits in order, and a call that would pass one is never made. It raises
`SpendRefused`.

1. The system prompt and the text together are at most 40,000 characters. A
   caller may pass another cap: a card sends a whole body.
2. The call's worst case is computed: every two characters of prompt count as a
   token, and every output token up to `max_tokens` is used. Spanish runs near
   four characters a token, so the bound is above what a call is billed.
3. The worst case must fit in the run's `Budget` when the caller passes one. The
   card commands always do. A call in flight counts against the budget at its
   worst case until it returns, so concurrent calls of one run cannot pass it
   together.
4. The worst case must fit under `DAILY_USD_CAP`, counted over every row of
   `llm_calls` since midnight in Lima. A transaction-level advisory lock
   serializes the check and the insert of the call's row, so processes that
   share the database cannot both pass the cap that only one fits under.

The row is written before the call with `reserved_usd`, the worst case. A call
in flight counts at that amount. When the call returns, the row gets the token
counts and the billed `usd`. When it fails, it gets the error and a cost:

- A `NotBilled` error counts 0. The Anthropic client raises it for an HTTP error
  status, which the API does not bill.
- Any other error, such as a timeout or a cancelled request, keeps the reserved
  cost, because the API may have billed the call.

A process that dies during a call leaves its row with no `usd`, and the row
counts at `reserved_usd` for the rest of the day.

`MODELS` in `client.py` holds each model's price per million tokens and the
setting that turns its thinking off. Thinking tokens count against `max_tokens`,
so a thinking model can spend the whole cap before it writes the reply. A model
missing from `MODELS` cannot be called, because its cost could not be bounded.
The client makes no retries, since each retry is billed again. Each `Prompt`
sets its own timeout: 60 seconds by default, 180 for a card. A timeout can fire
after the model ran, so it keeps the reserved cost.

Spend is a query away:

```sql
SELECT purpose, model, count(*), sum(coalesce(usd, reserved_usd))
FROM llm_calls
WHERE started_at >= now() - interval '1 day'
GROUP BY 1, 2;
```

The Anthropic Console limits the workspace too. Those limits are the operator's
and sit outside this code.

## Settings

| Variable            | Default | Meaning                                                   |
| ------------------- | ------- | --------------------------------------------------------- |
| `ANTHROPIC_API_KEY` | unset   | Without it the card commands exit                         |
| `DAILY_USD_CAP`     | `2.0`   | US dollars all processes together may spend in a Lima day |

## Tests

`apps/api/tests/fakemodel.py` stands in for the model. It records the prompts,
bills fixed token counts, and replies with a function of the prompt.
`test_llm.py` covers the quote rule, the client's request and errors, and the
spend limits against PostgreSQL. No test calls the Anthropic API.
