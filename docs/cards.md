# Cards

A case card says what one resolution decided, in four short fields a reader can
compare with their own case. Cards are written offline by a language model and
stored in the `cards` table. [`POST /cases`](cases.md) serves them with no model
call, so visitors never spend model credit. The code is in
`apps/api/src/reclamos_api/cards/`.

| File            | Owns                                                        |
| --------------- | ----------------------------------------------------------- |
| `card.py`       | The card prompt, its schema and the quote check of a reply  |
| `judge.py`      | The judge that reads each checked field against its quotes  |
| `repository.py` | The `cards` table: stale cards, writes, verdicts and report |
| `commands.py`   | `reclamos cards build`, `judge` and `report`                |

## The card

The card model reads the whole body of one resolution and replies in a JSON
schema with these fields:

| Field               | Says                                                         |
| ------------------- | ------------------------------------------------------------ |
| `claim`             | What the user claimed and asked for                          |
| `operator_position` | What the operator said or decided before the appeal or queja |
| `reasoning`         | Why the tribunal decided as it did                           |
| `evidence`          | The evidence that decided the case, or the evidence missing  |

Each field has a text and up to three quotes copied from the body. The reply
also holds three readings: `outcome`, `operator` and `resolved_on`, each with a
quote. The number, operator and outcome a page shows come from
[parse](corpus.md), never from the model. The readings exist to audit parse.

`check_card` keeps only what the body backs:

- A quote survives when it is in the body by the [quote rule](llm.md#quotes): at
  least 20 characters, comparing without case, accents, runs of whitespace or
  quote marks.
- A field with no surviving quote, or with no text, is dropped.
- A reading survives when its value is valid and its quote is in the body. An
  outcome must be one of the parse outcomes and a date must be a real date. A
  reading the model leaves empty has no quote, so it is dropped.
- Everything dropped goes to the card's `rejected` list, so the report can count
  it.

A reply cut at the token cap, or one that is not JSON, writes no card. The run
prints its resolution id as unreadable.

A card is current while its `prompt_version` and `body_sha256` match the code
and the body. `PROMPT_VERSION` hashes the system prompt and the schema, so a
prompt change makes every card stale. A re-parsed body makes its own card stale.
Writing a card again clears its verdict.

## Judge

`reclamos cards judge` sends each unjudged card to the judge model. The judge
reads the surviving fields and their quotes only, not the body. The quotes have
already passed the check, so a field its quotes back is backed by the body. The
judge lists each claim its quotes do not back, with its field. A card with an
empty list is faithful.

## Commands

The commands run on a host with `ANTHROPIC_API_KEY` set. Each model call goes
through the [spend limits](llm.md#spend-limits) and logs its row in `llm_calls`,
with purpose `card` or `judge`.

`reclamos cards build --budget-usd N` writes a card for every resolution with a
body and no current card, smallest id first. It stops before the call that could
take the run past `N` and prints where it stopped. A call in flight counts at
its worst case, so `--concurrency` cannot pass the budget.

| Flag              | Meaning                                   |
| ----------------- | ----------------------------------------- |
| `--budget-usd`    | The most the run may spend. Required      |
| `--concurrency N` | Calls in flight at once. The default is 4 |
| `--limit N`       | Write at most N cards (`build` only)      |

`reclamos cards judge --budget-usd N` takes the same flags but `--limit`.

`reclamos cards report` makes no call. It prints:

- the cards, how many are current, and their cost;
- how many cards kept each field;
- the quotes, fields and readings the check dropped;
- the share of judged cards the judge found faithful, and the unsupported claims
  by field;
- each reading that disagrees with parse, and each reading where parse has no
  value.

The operator reading matches parse when their letters match, so "América Móvil
Perú S.A.C." matches "AMERICA MOVIL PERU S.A.C.". A date matches on its value.

## Limits

A card prompt is at most 60,000 characters, above the longest body in the corpus
(47,053 characters, 2026-11-14). The reply is capped at 3,000 tokens and the
call times out after 180 seconds. A card call on the longest body reserves about
US$0.08 with Sonnet 5.5.

## Settings

| Variable      | Default             | Meaning          |
| ------------- | ------------------- | ---------------- |
| `CARD_MODEL`  | `claude-sonnet-5-5` | Writes the cards |
| `JUDGE_MODEL` | `claude-sonnet-5-5` | Judges the cards |

## Tests

`test_cards.py` builds cards from the fixture resolutions with the fake model.
It walks the flow: a run stopped by its budget, a second run that finishes, a
third that writes nothing, the quote check, the judge and the report. No test
calls the Anthropic API.
