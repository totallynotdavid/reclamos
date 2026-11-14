# Answers

`POST /answer` and `reclamos answers ask` answer a question in Spanish from the
passages [retrieval](retrieval.md) returns, or refuse. Every answer cites the
passages behind it, and each citation quotes its passage word for word. The code
is in `apps/api/src/reclamos_api/answers/`.

| File          | Owns                                                                |
| ------------- | ------------------------------------------------------------------- |
| `llm.py`      | The `LanguageModel` protocol, the Anthropic client and model prices |
| `spend.py`    | The cost log `llm_calls` and the limits checked before every call   |
| `answer.py`   | The prompt, the refusal rules and the citation check                |
| `judge.py`    | The faithfulness judge the evaluation runs                          |
| `api.py`      | The `POST /answer` route                                            |
| `commands.py` | `reclamos answers ask` and `reclamos answers eval`                  |

## Answer or refuse

`answer_question` in `answer.py` takes these steps:

1. Search with the question in hybrid mode and keep the best 6 passages that fit
   in 9,000 characters together.
2. Refuse with `no_passages` when nothing is found.
3. Refuse with `low_similarity` when the best passage's cosine similarity is
   below `REFUSAL_MIN_SIMILARITY`. No model call is made.
4. Send the question and the numbered passages to the answer model. The reply
   must follow a JSON schema: `supported`, `answer` and `citations`, each
   citation a passage number and a quote. The reply is capped at 900 tokens.
5. Refuse with `unverified` when the reply was cut at the token cap or is not
   JSON. Refuse with `model_declined` when the model says the passages do not
   answer.
6. Check each citation. Refuse with `unverified` when none survives.

A citation survives when its passage number names one of the passages sent and
its quote is at least 20 characters and is in that passage. The comparison
ignores case, accents, runs of whitespace and quote marks, because models
rewrite those. Any other difference drops the citation. A dropped citation does
not refuse the answer by itself, but the evaluation counts it.

The prompt tells the model to explain what the tribunal decided in the cited
cases and never to promise the outcome of the user's own case.

## Route

`POST /answer` takes `{"question": "..."}`. The question is 1 to 500 characters
after trimming, and any other field answers 422.

```json
{
  "question": "¿Me pueden cobrar un servicio que no contraté?",
  "outcome": "answered",
  "answer": "El tribunal declaró fundado el reclamo ... [1].",
  "reason": null,
  "citations": [
    {
      "passage": 1,
      "resolution_id": 4211,
      "expediente": "...",
      "number": "...",
      "date": "2024-03-18",
      "outcome": "fundado",
      "url": "https://www.gob.pe/...",
      "ordinal": 7,
      "page_start": 3,
      "page_end": 3,
      "quote": "..."
    }
  ]
}
```

A refusal has `"outcome": "refused"`, an empty `answer`, no citations and one of
the reasons above. The route answers 503 when no `ANTHROPIC_API_KEY` is set or
the index was embedded with another model. It answers 429 when the daily cap is
reached. The 429 does not say how much was spent.

The route has no rate limit of its own yet. The daily cap is the bound on what
visitors can spend.

## Spend limits

Every model call goes through `Ledger.complete` in `spend.py`. It checks these
limits in order, and a call that would pass one is never made. It raises
`SpendRefused`.

1. The system prompt and the text together are at most 40,000 characters.
2. The call's worst case is computed: every two characters of prompt count as a
   token, and every output token up to `max_tokens` is used. Spanish runs near
   four characters a token, so the bound is above what a call is billed.
3. The worst case must fit in the run's `Budget` when the caller passes one.
   `reclamos answers eval --budget-usd` does.
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

`MODELS` in `llm.py` holds each model's price per million tokens and the setting
that turns its thinking off. Thinking tokens count against `max_tokens`, so a
thinking model can spend the whole cap before it writes the reply. A model
missing from `MODELS` cannot be called, because its cost could not be bounded.
The client makes no retries, since each retry is billed again.

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

| Variable                 | Default             | Meaning                                                   |
| ------------------------ | ------------------- | --------------------------------------------------------- |
| `ANTHROPIC_API_KEY`      | unset               | Without it the route answers 503 and the commands exit    |
| `ANSWER_MODEL`           | `claude-haiku-5-5`  | Writes the answers                                        |
| `JUDGE_MODEL`            | `claude-sonnet-5-5` | Judges them in the evaluation                             |
| `DAILY_USD_CAP`          | `2.0`               | US dollars all processes together may spend in a Lima day |
| `REFUSAL_MIN_SIMILARITY` | `0.80`              | Best-passage similarity below which no call is made       |

`0.80` is provisional. The answers evaluation on the full corpus sets it.

## Commands

`reclamos answers ask "pregunta"` prints the answer, each citation with its
resolution and URL, the best similarity, the call's cost and the day's spend.

`reclamos answers eval --budget-usd N` answers the judged
[questions](evaluation.md#questions). It judges each answer with the judge
model, writes the results as JSON to `.data/answers-eval.json` and prints a
summary. It stops before the call that could take the run past `N`, and the
summary names the question it stopped at. Flags:

| Flag               | Meaning                              |
| ------------------ | ------------------------------------ |
| `--budget-usd`     | The most the run may spend. Required |
| `--limit N`        | Answer only the first N questions    |
| `--min-similarity` | The refusal threshold for this run   |
| `--no-judge`       | Skip the judge                       |
| `--questions`      | Another question file                |
| `--output`         | Another results path                 |

The summary reports:

- the share of answerable questions answered, and of those, the share that cite
  a relevant resolution;
- the share of judged answers the judge found faithful;
- the share of unanswerable questions refused, and the count of each refusal
  reason;
- the number of citations the check dropped, the latency at p50 and p95, and the
  run's spend.

The JSON also holds a sweep. For each threshold from 0.70 to 0.90 it gives the
share of answerable questions a threshold would keep and the share of
unanswerable ones it would refuse. Run with a low `--min-similarity` so every
question reaches the model, then read the threshold off the sweep. Only
thresholds at or above the run's own are meaningful.

## Tests

`apps/api/tests/fakemodel.py` stands in for the model. It records the prompts,
bills fixed token counts, and replies with a function of the prompt, so a test
can quote a passage the way a model would. `test_answers.py` covers the citation
check, each refusal, the spend limits against PostgreSQL, the route and the
evaluation's budget stop. No test calls the Anthropic API.
