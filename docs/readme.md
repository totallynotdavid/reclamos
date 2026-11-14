# Manual

1. [Architecture](architecture.md) maps the repository and runtime boundaries.
2. [Corpus](corpus.md) describes how TRASU resolutions are crawled, read, and
   counted.
3. [Retrieval](retrieval.md) describes the chunk model, the indexes, rank fusion
   and `POST /search`.
4. [Model calls](llm.md) describes the language model client, the quote rule and
   the spend limits.
5. [Cards](cards.md) describes the case cards written offline, their quote
   check, the judge and `reclamos cards`.
6. [Cases](cases.md) describes `POST /cases`, which serves similar resolutions
   and their cards, and its similarity threshold.
7. [Evaluation](evaluation.md) describes the judged questions, `reclamos eval`,
   the CI gate and the latest results.
8. [Operations](operations.md) covers local services, checks, and deployment
   verification.

The root [README](../README.md) is the quick start for readers who want to run
the project.
