# The evaluation harness

[ADR 0013](adr/0013-recommendation-engine.md) says no recommendation strategy ships
without numbers. This is how the numbers are produced: the harness replays votes that
already exist against a candidate strategy, offline, and prints a table.

```bash
cd server
uv run tindarr eval run                      # the author's 99 votes, the "popular" floor
uv run tindarr eval run --strategy hybrid    # the engine of ADR 0013
uv run tindarr eval run --check              # what CI does: fail when the numbers slid
uv run tindarr eval run --fixtures fixtures/eval/synthetic-99 --check   # the other set
```

Two vote sets are committed and both are gated in CI, for all three strategies. Every
table says which set it was measured on, on its second line; a number quoted without
that line means nothing.

It exists so that the fork's approach — the model invents titles, TMDb resolves them —
and the hybrid one — TMDb retrieves, the model picks — can be compared **before** either
is wired into the product. Since lot 4b all three strategies draw from the same
TMDb-retrieved pool, so the comparison is between the *choices* they make and not
between the shortlists they were handed.

## What it measures

The harness walks one person's votes in order. It reveals a prefix of them as history,
asks the strategy for a batch, and looks up every card it proposed in the votes it kept
back. Then it reveals the next slice and asks again.

Each proposed card ends up in exactly one of ten states. Four are **waste** — the
strategy was told about all of them in its context and proposed them anyway:

| State | Meaning |
|---|---|
| `duplicate` | the same title twice in one batch |
| `repeat` | already voted on before this batch |
| `served_again` | already served in an earlier batch of this run |
| `owned` | already in the household's library |

Five are the user's own words (`like`, `dislike`, `seen_liked`, `seen_disliked`,
`skip`), and the tenth is `unknown`: **this fixture never asked them.** An unknown card
is not a hit and not a miss. It is counted as the gap in coverage that it is.

| Metric | Better | Basis | Kind | What it is |
|---|---|---|---|---|
| `fill_rate` | higher | pool-free | measured | cards returned over cards asked for |
| `usable_per_batch` | higher | pool-free | measured | cards per batch that could really have been shown |
| `complete_rate` | higher | pool-free | measured | usable cards handed back with the details a card is built from |
| `waste_rate` | lower | pool-free | measured | proposed cards the strategy had been told to avoid |
| `liked_recall` | higher | pool-free | measured | titles the user liked that the strategy found at all |
| `liked_recall_top` | higher | pool-free | measured | the same, counting only the first three cards of a batch |
| `seen_per_batch` | lower | pool-free | measured | cards per batch the user had already watched |
| `disliked_per_batch` | lower | pool-free | measured | cards per batch the user turned down |
| `coverage` | — | pool-free | measured | usable cards the fixture has an opinion on |
| `catalogue_coverage` | — | pool-free | measured | usable cards the fixture can describe |
| `already_seen_rate` | lower | votes | measured | scored cards the user had already watched (**the fork: 47 %**) |
| `like_rate` | higher | votes | measured | scored cards the user wanted, and had not seen |
| `new_like_rate` | higher | votes | measured | likes among the cards new to them (**the fork: 63 %**) |
| `agreement` | — | votes | measured | scored cards the user liked, already seen or not |
| `skip_rate` | lower | votes | measured | judged cards the user had no opinion on |
| `genre_diversity` | higher | catalogue | measured | distinct genres per card the batch was asked for |
| `franchise_repeat_rate` | lower | catalogue | measured | batches serving the same franchise twice |
| `tmdb_calls_per_batch` | lower | pool-free | measured | metadata calls a batch cost |
| `llm_calls_per_batch` | lower | pool-free | measured | model calls a batch cost |
| `llm_tokens_per_card` | lower | pool-free | **estimated** | tokens per usable card |

### The basis column, and why an open pool needs it

Until lot 4b every strategy drew its candidates from the fixture's own catalogue, which
on a vote set built from real votes holds exactly the titles somebody voted on. Every
card was therefore scoreable, and `coverage` was 100 %. **The retrieval layer draws from
TMDb**, so most proposed titles have no vote at all: coverage collapses to a few per
cent, and every rate whose denominator is "the cards this fixture has an opinion on" is
suddenly computed over a handful of cards. A rate on five cards and the same rate on
sixty are not comparable numbers, whatever the table says.

So each metric declares what it needs:

- **`pool-free`** — meaningful whatever the pool was. Either it divides by what the
  strategy was *asked* for (`fill_rate`, `waste_rate`, the cost rows) or by a
  denominator the vote set fixes before the run (the two recalls), or it is a count
  rather than a rate (`seen_per_batch`, `disliked_per_batch`).
- **`votes`** — divides by the proposed cards the fixture voted on.
- **`catalogue`** — needs the fixture to *describe* the proposed cards; with an open
  pool it usually cannot.

With fewer than **twelve cards** behind it, a number is printed with a `?`, named in
the report's notes, and **not compared with another strategy's**. A count rather than a
share, because the share is not what makes a percentage a fiction: below a dozen cards
one card is worth more than eight points, and the gate's tolerance is two. The build
says which comparisons it declined, every time — a gate nobody knows is switched off is
worse than no gate.

Shrinking the denominator to escape a comparison does not help, and that is the point.
The pool-free metrics go on being graded: a strategy proposing titles nobody voted on
scores nothing on `liked_recall`, and `seen_per_batch` keeps counting the cards it
wasted.

### Recall and avoidance: the two that an open pool cannot dilute

**`liked_recall`** answers the question a rate cannot: *of the titles this person liked,
how many did the strategy ever put in front of them?* The denominator is counted before
the first batch — the `like` votes among the ones the replay is withholding — so it is a
property of the vote set, identical for every strategy walked with the same options. A
strategy that reaches past the fixture does not shrink it; it simply fails to find
anything. `liked_recall_top` is the same number counting only the first three cards of a
batch, because a deck is answered from the front. The report also prints the recall
batch by batch, so "finds them immediately" and "finds them once it has run out of
obvious picks" are different results.

Only `like` votes count, never `seen_liked`: a title the user had already watched is not
a find, it is the fault below.

**`seen_per_batch`** and **`disliked_per_batch`** are counts, deliberately. Serving a
title the user had already watched, or one they turned down, costs a swipe whether or
not the nine cards beside it happened to be scoreable — dividing that fault by a
denominator an open pool empties is exactly how it disappears. ADR 0013's 47 % is 4.7
already-seen cards in a batch of ten; this is that number, in cards.

**They hold each other up.** Recall alone is beaten by proposing everything in sight;
the avoidance counts alone are beaten by proposing obscure titles nobody has an opinion
on — which scores zero recall. And `fill_rate` and `usable_per_batch` are graded too, so
a strategy cannot make the counts small by shrinking the batch.

Everything is counted against the fixture except one thing, and the table says which:
token counts are **measured** when the AI provider reports them and **estimated** at four
characters per token when it does not (Ollama and several OpenAI-compatible endpoints
report nothing). The column only says "estimated" when a provider really withheld them,
and the report says how many calls that was.

Two of those metrics exist because of what a strategy could otherwise get away with.
`complete_rate` is there so that "made no TMDb call" stops being a perfect score on the
cost axis for cards that cannot be rendered. `skip_rate` divides by the cards the fixture
has an opinion on rather than by every card that was not waste, so padding a batch with
titles nobody voted on cannot drive it to zero; `genre_diversity` divides by the cards the
batch was *asked* for, so three cards cannot out-diversify ten.

**Three metrics are printed and not graded**, and the reason matters. `coverage` and
`catalogue_coverage` fall when a strategy reaches past the recorded history, which is
not a fault — it is the basis column's input. `agreement` counts `seen_liked` as a hit —
the user did like the film — so the strategy ADR 0013 asks for, the one that stops
serving titles people have already watched, will *lower* it. Grading any of them would
make the gate punish the improvement it exists to protect.

Every rate is printed with its denominator, and the gate reads the denominators too
(below). A rate without its denominator is a number that can be gamed.

## What it cannot measure

- **The votes were cast on another engine's cards.** A candidate strategy proposing a
  title the recorded engine never showed cannot be scored, and the user's history does
  not change in response to what the candidate proposed. This compares strategies against
  one fixed history; it does not simulate a person.
- **Recall is therefore biased towards strategies that resemble the engine that produced
  the votes.** The fork proposed famous titles, so the only titles the fixture can
  confirm a strategy "found" are famous ones — and a strategy deliberately reaching past
  the famous end, which is what ADR 0013 asks for, is penalised for doing so. It is the
  sharpest form of the point above, and it is the reason a high recall on `akira-99` is
  not on its own evidence of a better deck.
- **A model is not deterministic, and at these coverages one card is worth points.**
  `akira-99` confirms six to nine of ninety proposed cards, so one title found or missed
  moves `liked_recall` by 3.3 points — more than the gate's tolerance. The committed
  numbers are exact, because the replay reads a recorded answer; what they are not is
  *repeatable under a new recording*. Two configurations that differ by one or two cards
  on this vote set have not been told apart.
- **A vote cast later is used as the opinion at this point.** Tastes move. Over a few
  months of votes this is a small lie; over years it would not be.
- **The rates rest on tens of cards, not thousands, and since lot 4b on fewer.** With a
  TMDb pool, coverage is around 20 % on `synthetic-99` — whose "TMDb" is its own invented
  catalogue — and around 2 % on `akira-99`, where the pool really is TMDb and the fixture
  has an opinion on 99 titles out of it. The report says so in its notes, marks the
  affected rows, and the gate stops comparing them. That is the trade lot 4b made
  deliberately: a strategy measured on a realistic pool, with fewer numbers that mean
  anything, rather than a strategy measured on a shortlist of titles somebody already
  voted on.
- **The avoidance counts are a floor, not the truth.** `seen_per_batch` counts the cards
  the user *declared* they had already watched. With an open pool most proposed titles
  were never put in front of them, so a title they have seen and never voted on is
  counted as nothing. Every card it does count is a real fault, and no strategy can make
  the number look better by proposing more — but "zero already-seen cards" means "none
  that this fixture can prove", which on `akira-99` is a much weaker claim than it
  sounds. `liked_recall` has no such asymmetry: its denominator enumerates every liked
  title the replay withheld, so it is exact.
- **The fixture cannot describe what it never voted on.** `catalogue_coverage` falls
  with coverage, and the two diversity metrics fall with it, because genres and
  franchises are read from the fixture's catalogue and never from what a strategy said
  about its own picks. On `akira-99` they are computed over the two or three proposed
  cards the fixture happens to know, and the basis column says so.
- **A fixture's taste profile is static.** In production it is rewritten every ten votes,
  so it only ever knows the past; a fixture carries one profile for the whole history. In
  a *generated* fixture it would name the very genres the later votes were drawn from —
  the answer key — so neither committed vote set carries one at all and the gate is
  profile-blind. An imported one may keep its own, written by a real engine from real
  votes, which carries no such leak.
- **It says nothing about prose.** Whether a rationale reads well, whether a poster
  loads, whether the deck feels good in the hand: none of that is here.
- **It cannot prove a strategy is good.** It can show that one is worse than another on
  one recorded history, which is all ADR 0013 asked for.

## How to read the output

```
already_seen_rate         85.0%  lower   votes     measured  scored cards the user had already watched (ADR 0013: 47 %)

3 users, 9 batches, 90/90 cards proposed
usable 90 (wasted 0: 0 duplicate, 0 already voted, 0 already served, 0 owned)
scored 20 (1 like, 2 dislike, 16 seen+liked, 1 seen+disliked), skipped 1, no vote 69
found 1 of the 8 titles these users liked (0 in the first 3 cards of a batch); by batch: 0, 1, 0, …
cost 90 metadata calls, 0 model calls, 0 tokens
```

(that one is a `synthetic-99` run: three users, so three vote histories.)

Read the counts first. `scored` is the denominator of every `votes`-basis rate in the
table above it; if it is small, those rates are indicative and nothing more, and the
table marks them with a `?`. `no vote` is the coverage gap. `wasted` should be zero for
any strategy that reads its own context. The `found …` line is recall with its own
denominator beside it, which is the line to read when coverage has collapsed.

`--json <path>` writes the same report as a JSON document, sorted and stable, for a diff
or a dashboard.

## The vote sets

Two of them, each in its own directory under `server/fixtures/eval/`, named after the
set it holds — the same name the report prints on its second line. Each directory holds
the same four files:

- `votes.json` — the vote set: a catalogue of titles and one vote history per user;
- `tmdb.json` — a **cassette**, the TMDb answers an offline run replays;
- `llm.json` — the same for the model answers a strategy that calls one replays;
- `baseline-popular.json`, `baseline-random.json`, `baseline-hybrid.json` — the numbers
  CI holds a run to.

`--fixtures` picks one, and both are gated in CI against their own committed baselines.
A baseline records the vote set it was measured on, so the gate refuses to hold a run of
one set to the numbers of the other.

### `akira-99` — one person's real votes

`server/fixtures/eval/akira-99/` holds **99 real votes, cast by Tindarr's author in the
SuggestArr fork's swipe deck and published here with their consent.** This is the vote
set ADR 0013 was written from: 47 % of the opinions are "I had already seen this", and
63 % of the genuinely new titles were liked. A test replays the recorded cards back
through the harness and checks that both numbers come out again. Anything else means the
scoring is wrong, and the fix would then be in the scoring — never in the expectation.

Published is the minimum a replay needs: the TMDb id, the media type, the verdict, the
rank of the vote in the history, and which kind of pick produced the card. Not published:
the account, every timestamp, the taste profile the engine had written, the household's
library, and every word the model generated. The catalogue beside the votes — titles,
years, genres, franchises, popularity — is TMDb's own data about public films and series,
and says nothing about a person.

**What one person's votes can and cannot support.** This is one household, one taste, a
few evenings of swiping, 99 opinions. The rates rest on tens of cards: enough to catch a
mis-scoring harness and a strategy that is obviously worse, not enough to separate two
good ones, and no evidence at all about anybody else's taste. The bias is not only
statistical either — these are the votes of the person writing the recommender, which is
the one taste a recommender is least likely to get wrong by accident. **A second vote
set, from somebody else, would do more for these numbers than any amount of tuning.**

Its cassette was recorded once, from the real TMDb:

```bash
TINDARR_EVAL_TMDB_API_KEY=… uv run tindarr eval record --fixtures fixtures/eval/akira-99
```

That says what it is about to call and waits for an answer, like every live path here. It
records the **whole catalogue** rather than whatever one strategy happened to ask for, so
the file is a function of the vote set and the next strategy needs no live run of its own.
Nothing can regenerate it, so a test holds it to the only property that still means
something: it answers, with a 200, for every title an offline replay can be asked about.

### `synthetic-99` — the generated set

`server/fixtures/eval/synthetic-99/` is invented, and so is everything in it: the titles
do not exist and the TMDb ids are in a range (from 900001) that TMDb does not use.

It is kept, because it does two things the real set cannot. It is **reproducible** —
`eval fixtures` rebuilds it from a seed and a test holds the committed bytes to what the
generator produces — so the repository is not left with fixtures nobody can rebuild. And
it carries **three users and six skips**, neither of which the real set has, which is how
the per-user reveal schedule, and the rule that a skip must change no rate, stay tested.

```bash
uv run tindarr eval fixtures      # rebuild votes.json and tmdb.json from the seed
```

It was given the same distribution as the real set — 99 opinions, 47 % already seen, 63 %
of the new ones liked — so the same sanity check runs on both. **But a generated fixture
cannot surprise anybody.** It has exactly the distribution it was handed. It says whether
the harness computes what it claims to; it says nothing about whether a strategy will
please a real person.

`eval fixtures` refuses to write into a directory holding a vote set it did not generate:
the two directories carry the same file names, and only one of them can be rebuilt.

### A third: your own

The most useful vote set is the one on your instance:

```bash
uv run tindarr eval import --from /path/to/suggestarr.db      # or Tindarr's own database
uv run tindarr eval record --fixtures fixtures/eval/private
uv run tindarr eval run --fixtures fixtures/eval/private
```

The import reads the database **read-only**, keeps the TMDb id, the media type, the vote
and the order, and drops the account ids (users become `user-1`, `user-2`…), the model's
rationales, poster paths and every timestamp. It refuses to write anywhere but a
directory called `private/`, which `.gitignore` keeps out of the repository. An imported
vote set is somebody's viewing history: publishing one is *their* decision, and the
harness never makes it for them.

## No paid calls

The default path makes **no network call at all**. The TMDb adapter — the real one, not a
stand-in — talks through the recorded cassette, and a request the cassette does not hold
raises `CassetteMissError` instead of falling through to the internet. An offline run
cannot quietly become a paid one.

`--live` (the real TMDb), `--live-llm` (the real AI provider) and `eval record` are the
only ways out. They are separate taps, so a model strategy can be re-recorded without
touching TMDb, and all of them say what they are about to do first:

```
A live run reaches real services. This one would call:
  TMDb        up to 90 requests (9 batches of 10). TMDb's API is free for personal use;
              its rate limits and caching terms apply.
  AI provider none: this strategy calls no model, so nothing is billed.
```

It then waits for an answer at the terminal, or for `--yes`. The TMDb key comes from
`TINDARR_EVAL_TMDB_API_KEY` — never from the instance's database. The model endpoint
comes from `TINDARR_EVAL_LLM_BASE_URL` (`TINDARR_EVAL_LLM_MODEL`,
`TINDARR_EVAL_LLM_API_KEY`), and it is always the **OpenAI-compatible** kind: the
harness is a development command, and a default that reaches a named vendor is a default
that bills somebody by accident. The plan prints the address before anything is sent.

`--record <dir>` **merges** what came back into that directory's cassettes — one live run
per strategy, each adding the pages and the details it asked for, so a second run does
not throw away the first one's answers. Delete the file to record from nothing.

The credential is stripped before a request becomes a cassette key, so a recording is
safe to keep and survives a key rotation. A model recording goes one step further: the
endpoint's own address is rewritten to `http://recorded.invalid/v1` before the file is
written, because an OpenAI-compatible endpoint is somebody's own machine and its host
name has no business in a public repository — and a cassette keyed on it would replay
for nobody else.

```bash
cd server
TINDARR_EVAL_TMDB_API_KEY=… TINDARR_EVAL_LLM_BASE_URL=http://…/v1 \
  uv run tindarr eval run --fixtures fixtures/eval/akira-99 --strategy hybrid \
  --live --live-llm --record fixtures/eval/akira-99
```

**A model is not deterministic.** Two live runs of the same batch produce different
cards, so the committed numbers are the numbers of *the run that was recorded*, and the
cassette is what makes them reproducible. Re-recording a model strategy is therefore a
re-measurement, not a refresh: the baseline moves with it, and the commit has to say so.

## The CI gate

`server.yml` runs `tindarr eval run --check` for every committed baseline — that is both
vote sets times all three strategies, six runs. A baseline holds the vote set it was measured
on, the strategy, the replay options, the metrics and the tolerances. `--check` asks two
questions, because one is not enough: *did this strategy get worse than it was?* (its own
baseline) and *is it better than doing nothing clever?* (the floors).

The check fails the build when:

- a graded metric slides past its tolerance — two points of percentage for a rate, one
  call per batch for TMDb, a quarter of a call for the model;
- a metric that had a value becomes `n/a`, because its denominator emptied;
- a graded metric is **missing** from the baseline, which means nobody has measured it
  yet rather than that everything is fine;
- `batches`, `usable` or `scored` falls by more than 2 %;
- the strategy fails to clear **one whole reference floor** (`popular` or `random`) —
  that is, there is no floor it is no worse than on every graded metric whose basis both
  runs support. The floors themselves are exempt: they are the yardstick, and each is
  worse than the other somewhere. Every comparison the gate declines is printed with the
  result, under `floor comparisons not made`.

  It is **one floor whole**, not the best value of each metric across the floors. The
  composite of two floors is a strategy that does not exist and that neither floor
  clears: `popular` is better at likes, `random` at not repeating what somebody has
  already seen, and holding a candidate to both at once is holding it to a bar the
  yardstick itself fails. What the gate means to ask is "is this better than doing
  something stupid?", and beating a whole reference strategy is that question.

- **What a strategy spends is exempt from the floor**, and only from the floor. A floor
  that calls no model reports zero model calls; requiring a strategy that calls one to
  be "no worse than the floors" on that row would be requiring it not to exist. Cost is
  gated by the strategy's own baseline — a quarter of a model call, fifty tokens a card,
  one TMDb call a batch — and printed by the live plan before a run spends anything.

The floor rule and the count rule are the ones that matter. The cheapest way to improve
every rate is to propose fewer cards — three confident picks instead of ten score
beautifully — so the denominators are gated. And without the floor, the gate would only be a per-strategy
regression test: the first baseline a new strategy writes is whatever it happened to
score, so a strategy worse than "show the most popular thing you have not voted on" could
certify itself and stay green for ever.

**The tolerances come from the code, not from the baseline file.** They are written into
the file so a reader can see them, and ignored when checking: a gate whose thresholds live
inside the thing it guards is switched off by a one-token diff that looks like a number.
A cost that was zero gets no allowance at all, so the first model call a strategy makes is
a change the build shows you.

The comparison is refused outright — not quietly allowed — when the baseline is about a
different vote set, a different strategy or different replay options.

### Updating a baseline

Deliberately, with a reason:

```bash
cd server
uv run tindarr eval run --strategy popular --update-baseline
uv run tindarr eval run --strategy popular --check   # the floors still have to be clear
git diff server/fixtures/eval/akira-99/baseline-popular.json
```

`--update-baseline` and `--check` cannot be given together: a run that writes the numbers
it is then compared to is a check that cannot fail.

The diff shows every number that moved. Commit it with the change that moved them and say
in the message why the new numbers are the right ones. A baseline bumped in its own commit,
with no explanation, is a gate that has been switched off.

## What lot 4b measured

Six committed runs: three strategies on two vote sets, all replayed from the cassettes,
so every number below reproduces exactly. Read the `pool-free` rows first; the rest are
marked `?` in the report wherever fewer than twelve cards went into them, and the gate
does not compare those.

**`synthetic-99`** (coverage 13–22 %, so the rates still mean something):

| | `popular` | `random` | **`hybrid`** |
|---|---|---|---|
| `liked_recall` | 8.7 % | **21.7 %** | 17.4 % |
| `liked_recall_top` | 0.0 % | **4.3 %** | **4.3 %** |
| `seen_per_batch` | 1.67 | 0.89 | **0.78** |
| `disliked_per_batch` | 0.22 | **0.11** | **0.11** |
| `fill_rate` | 96.7 % | 96.7 % | **100 %** |
| `already_seen_rate` | 78.9 % | **57.1 %** | 58.3 % |
| `like_rate` | 10.5 % | **35.7 %** | 33.3 % |
| `genre_diversity` | 0.91 | 0.88 | **1.02** |
| `franchise_repeat_rate` | 33.3 % | 44.4 % | **22.2 %** |
| `tmdb_calls_per_batch` | **18.1** | **18.1** | 19.8 |
| `llm_tokens_per_card` | **0** | **0** | 299 |

**`akira-99`** (coverage 2–8 %: only the four `pool-free` rows are compared):

| | `popular` | `random` | **`hybrid`** |
|---|---|---|---|
| `liked_recall` | **6.7 %** (2/30) | 0.0 % (0/30) | 3.3 % (1/30) |
| `liked_recall_top` | 0.0 % | 0.0 % | **3.3 %** |
| `seen_per_batch` | 0.00 | 0.22 | 0.56 |
| `fill_rate` | 100 % | 100 % | 100 % |
| `tmdb_calls_per_batch` | **16.9** | **16.9** | 17.3 |

**What that says, and what it does not.** On the generated set the hybrid beats
`popular` on every graded metric and loses to `random` on recall and the like rate by
about one card. It clears `popular` whole, which is what the gate asks. On the author's
real votes nothing is settled: seven of its ninety cards carry a vote, so `liked_recall`
moves 3.3 points per title found, `seen_per_batch` counts only faults the fixture can
confirm, and the strategy that scores best on recall there is the one that behaves most
like the engine that produced the votes. Its one clear win on that set is
`liked_recall_top`: the single liked title it found, it put in the first three cards.

**The already-seen number ADR 0013 is about is not answered yet.** The fork served 4.7
already-seen cards a batch; the hybrid serves 0.78 on the generated set and 0.56 on the
real one — but the fork's figure was measured against a person answering, and these are
measured against a fixture that can only recognise 99 titles. They are not the same
measurement and should not be quoted as one. What would make them comparable is a deck
in front of a person (step 5) or a second, larger vote set.

## The strategy port

A strategy is anything with a name and one method
(`server/src/tindarr/swipe/strategy.py`):

```python
async def propose(self, context: StrategyContext, size: int) -> Sequence[Candidate]
```

The harness measures a strategy; it does not sandbox one. Both run in the same process,
so code that is determined to lie about what it spent can. What the harness does is make
that deliberate rather than convenient — the cost counters are read-only, a total that
goes backwards stops the run, "owned" is scored against a snapshot the strategy is not
handed, and the numbers that actually guard the gate are ones a strategy cannot reach at
all: whether its cards carry their details, and whether it beats the floors.

`StrategyContext` is the whole input — taste profile, past votes, library, engagement,
cards already served, novelty, mood, media type, content filters, language, region, and a
seed for anything that would otherwise be random. A strategy reads nothing else: no
database, no clock, no global. That is what makes the replay meaningful, since the harness
can hand it the state of the world as it was at some past moment and be sure nothing from
after that moment leaked in.

`history` holds the votes cast **before** the batch being asked for, and nothing else. A
strategy is built once per user, so no cache carries one person's answers into another's
batch.

Three implementations ship, and **all three draw from the same candidate pool**
(`tindarr.swipe.retrieval`). That matters: while the floors drew from the fixture's own
catalogue and a candidate drew from TMDb, "is the model better than picking the most
popular thing?" was being asked of two strategies that had been handed different
questions.

- `popular` — the most popular candidate in the pool. Knows nothing about taste,
  everything about fame: the defect ADR 0013 is about, in one line of code.
- `random` — a seeded draw from the same pool. Nothing should ever score below it.
- `hybrid` — the engine of ADR 0013 (`tindarr.swipe.hybrid`): the model is handed the
  pool — one line per candidate, with its id, title, year, **genres**, rating and vote
  count — and answers with ids, a rationale per card and a pick kind. An id it was not
  offered is dropped; a short answer is topped up from the pool; a model that fails
  costs the sentences and not the batch. The prompt is the fork's, minus its "never
  propose" list: the pool has already made it unnecessary.

Adding one means writing the class, registering it in `STRATEGIES`
(`server/src/tindarr/main/evaluation.py`) with what a live run of it would cost, and
committing its baseline.

## The candidate pool

`tindarr.swipe.retrieval` builds it, per user, from two TMDb endpoints:

- **`/{kind}/{id}/recommendations`** for each of the user's six most recent likes,
  twelve deep — "people who liked this went on to watch";
- **`/discover/{kind}`**, pages chosen by the novelty band, with the band's vote-count
  window and the household's excluded genres pushed down to TMDb.

The two are interleaved in the proportion the novelty level asks for, so a batch taken
off the front of the pool has the safe/explore mix the fork used to impose by reshuffling
the model's answer afterwards.

**The novelty band.** ADR 0013 asks for an adaptive popularity floor: the bolder the
setting, the lower the popularity a candidate may have. It is here, together with a
**vote-count window** — how many people ever had an opinion about a title, which is a
far steadier number than TMDb's rolling `popularity` and a better proxy for "they have
probably already seen it". Its bottom end keeps listings and home videos out of a deck;
its top end, at `bold`, is what pushes back on ADR 0013's 47 %. The discovery pages do
the rest: page one of "most popular" *is* the wall of blockbusters, and reaching past it
is most of what novelty means.

| Novelty | Popularity floor | Vote window | Discovery pages | From their likes |
|---|---|---|---|---|
| `familiar` | 5 | 600+ | 1–2 | 70 % |
| `balanced` | 2 | 150+ | 1–3 | 50 % |
| `bold` | 0 | 40–4000 | 2–4 | 35 % |

**One idea the harness refused.** A *fame cut* — dropping the most popular quarter of
the retrieved pool at `balanced` and nearly half at `bold` — was written, measured and
removed. It is not in ADR 0013, it was this lot's own idea, and on both vote sets it
cost recall without moving the already-seen count by more than a card, which is inside
the noise at these coverages. An unmeasured mechanism in a recommender is a mechanism
nobody can remove later, so it went. The numbers that refused it are in the commit that
removed it.

A calibration batch reverses all of it: it is trying to find out what somebody has
*already* watched, so it reads the familiar band, sorted by vote count.

**Every exclusion is applied to the pool**, never to the model's answer: voted on,
already served, owned, wrong media type, adult, before `min_year`, an excluded original
language, an excluded genre. Filtering the answer instead would mean paying for cards
that are then thrown away, and it would rest on a model honouring a "never propose" list
— which is what the fork did, and what its numbers measure. The strategies re-apply the
check on the way out anyway, on both paths, because the day it can fail is the day a
retrieval bug puts a title somebody already voted on in front of them.

A TMDb call the pool needs and does not get — a deleted id, a page that times out — is
logged and skipped. The pool is smaller; the batch still ships.
