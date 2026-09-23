# The evaluation harness

[ADR 0013](adr/0013-recommendation-engine.md) says no recommendation strategy ships
without numbers. This is how the numbers are produced: the harness replays votes that
already exist against a candidate strategy, offline, and prints a table.

It exists so that the fork's approach — the model invents titles, TMDb resolves them —
and the hybrid one — TMDb retrieves, the model picks — can be compared **before** either
is wired into the product.

```bash
cd server
uv run tindarr eval run                      # the committed vote set, the "popular" floor
uv run tindarr eval run --strategy random
uv run tindarr eval run --check              # what CI does: fail when the numbers slid
```

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

| Metric | Better | Kind | What it is |
|---|---|---|---|
| `fill_rate` | higher | measured | cards returned over cards asked for |
| `usable_per_batch` | higher | measured | cards per batch that could really have been shown |
| `waste_rate` | lower | measured | proposed cards the strategy had been told to avoid |
| `coverage` | — | measured | usable cards the fixture has an opinion on |
| `already_seen_rate` | lower | measured | scored cards the user had already watched (**the fork: 47 %**) |
| `like_rate` | higher | measured | scored cards the user wanted, and had not seen |
| `new_like_rate` | higher | measured | likes among the cards new to them (**the fork: 63 %**) |
| `agreement` | — | measured | scored cards the user liked, already seen or not |
| `skip_rate` | lower | measured | usable cards the user had no opinion on |
| `genre_diversity` | higher | measured | distinct genres per catalogued card in a batch |
| `franchise_repeat_rate` | lower | measured | batches serving the same franchise twice |
| `tmdb_calls_per_batch` | lower | measured | metadata calls a batch cost |
| `llm_calls_per_batch` | lower | measured | model calls a batch cost |
| `llm_tokens_per_card` | lower | **estimated** | tokens per usable card |

Everything is counted against the fixture except one thing, and the table says which:
token counts are **measured** when the AI provider reports them and **estimated** at four
characters per token when it does not (Ollama and several OpenAI-compatible endpoints
report nothing). The report says how many calls it had to estimate.

**Two metrics are printed and not graded**, and the reason matters. `coverage` falls when
a strategy reaches past the recorded history, which is not a fault. `agreement` counts
`seen_liked` as a hit — the user did like the film — so the strategy ADR 0013 asks for,
the one that stops serving titles people have already watched, will *lower* it. Grading
either would make the gate punish the improvement it exists to protect.

Every rate is printed with its denominator, and the gate reads the denominators too
(below). A rate without its denominator is a number that can be gamed.

## What it cannot measure

- **The votes were cast on another engine's cards.** A candidate strategy proposing a
  title the recorded engine never showed cannot be scored, and the user's history does
  not change in response to what the candidate proposed. This compares strategies against
  one fixed history; it does not simulate a person.
- **A vote cast later is used as the opinion at this point.** Tastes move. Over a few
  months of votes this is a small lie; over years it would not be.
- **Coverage is low on a fixture with a large catalogue**, around 20 % on the committed
  one. The rates rest on tens of cards, not thousands. The report says so in its own
  notes whenever coverage is under half.
- **The taste profile in a fixture is static, and in a generated one it is the answer.**
  In production the profile is rewritten every ten votes, so it only ever knows the past.
  A fixture carries one profile for the whole history; in the generated one it names the
  genres the later votes were drawn from. A strategy that reads the profile therefore
  scores better here than it would in life. The private fixtures, whose profiles were
  written by a real engine from real votes, do not have this problem.
- **It says nothing about prose.** Whether a rationale reads well, whether a poster
  loads, whether the deck feels good in the hand: none of that is here.
- **It cannot prove a strategy is good.** It can show that one is worse than another on
  one recorded history, which is all ADR 0013 asked for.

## How to read the output

```
already_seen_rate         85.0%  lower   measured  scored cards the user had already watched (ADR 0013: 47 %)

3 users, 9 batches, 90/90 cards proposed
usable 90 (wasted 0: 0 duplicate, 0 already voted, 0 already served, 0 owned)
scored 20 (1 like, 2 dislike, 16 seen+liked, 1 seen+disliked), skipped 1, no vote 69
cost 90 metadata calls, 0 model calls, 0 tokens
```

Read the counts first. `scored` is the denominator of every rate in the table above it;
if it is small, the rates are indicative and nothing more. `no vote` is the coverage gap.
`wasted` should be zero for any strategy that reads its own context.

`--json <path>` writes the same report as a JSON document, sorted and stable, for a diff
or a dashboard.

## The fixtures

`server/fixtures/eval/` holds three committed files:

- `votes.json` — the vote set: a catalogue of titles and one vote history per user;
- `tmdb.json` — a **cassette**, the TMDb answers an offline run replays;
- `baseline-popular.json`, `baseline-random.json` — the numbers CI holds a run to.

The committed vote set is **generated**, and everything in it is invented: the titles do
not exist and the TMDb ids are in a range (from 900001) that TMDb does not use. Real
votes are a list of what somebody watched and what they thought of it, which is not
something a public repository should carry.

What is *not* invented is the shape. The distribution is the one ADR 0013 was measured
on — 99 opinion votes, 47 % of them "I had already seen this", 63 % of the genuinely new
ones liked — plus six skips, which carry no opinion and must change no rate. A test
replays the recorded cards back through the harness and checks that both numbers come
out again; anything else means the scoring is wrong.

```bash
uv run tindarr eval fixtures      # rebuild votes.json and tmdb.json from the seed
```

A test holds the committed files to what the generator produces. A fixture nobody can
regenerate is a fixture nobody can review.

**A generated fixture cannot surprise anybody.** It has the distribution it was given, so
it says whether the harness computes what it claims to; it does not say whether a strategy
will please a real person. For that, point the harness at a real instance:

```bash
uv run tindarr eval import --from /path/to/suggestarr.db      # or Tindarr's own database
uv run tindarr eval run --fixtures fixtures/eval/private
```

The import reads the database **read-only**, keeps the TMDb id, the media type, the vote
and the order, and drops the account ids (users become `user-1`, `user-2`…), the model's
rationales, poster paths and every timestamp. It refuses to write anywhere but a
directory called `private/`, which `.gitignore` keeps out of the repository. An imported
vote set is somebody's viewing history; it stays on their machine.

**A second, larger vote set would make all of this more robust.** One history of a
hundred votes is enough to catch a mis-scoring harness and a strategy that is obviously
worse; it is not enough to separate two good strategies. Anybody running Tindarr can
produce one with `eval import`, and the numbers from several private runs are worth more
than the committed one.

## No paid calls

The default path makes **no network call at all**. The TMDb adapter — the real one, not a
stand-in — talks through the recorded cassette, and a request the cassette does not hold
raises `CassetteMissError` instead of falling through to the internet. An offline run
cannot quietly become a paid one.

`--live` is the only way out, and it says what it is about to do first:

```
A live run reaches real services. This one would call:
  TMDb        up to 90 requests (9 batches of 10). TMDb's API is free for personal use;
              its rate limits and caching terms apply.
  AI provider none: this strategy calls no model, so nothing is billed.
```

It then waits for an answer at the terminal, or for `--yes`. A live run reads its TMDb
key from `TINDARR_EVAL_TMDB_API_KEY` — never from the instance's database — and
`--record <path>` writes what came back as a new cassette. The credential is stripped
before a request becomes a cassette key, so a recording is safe to keep and survives a
key rotation.

## The CI gate

`server.yml` runs `tindarr eval run --check` for every committed baseline. A baseline
holds the vote set it was measured on, the strategy, the replay options, the metrics and
the tolerances. The check fails the build when:

- a graded metric slides past its tolerance — two points of percentage for a rate, one
  call per batch for TMDb, a quarter of a call for the model;
- a metric that had a value becomes `n/a`, because its denominator emptied;
- `batches`, `usable` or `scored` falls by more than 2 %.

That last rule is the one that matters. The cheapest way to improve every rate is to
propose fewer cards: three confident picks instead of ten score beautifully. The
denominators are gated so that they cannot.

The comparison is refused outright — not quietly allowed — when the baseline is about a
different vote set, a different strategy or different replay options.

### Updating a baseline

Deliberately, with a reason:

```bash
cd server
uv run tindarr eval run --strategy popular --update-baseline
git diff server/fixtures/eval/baseline-popular.json
```

The diff shows every number that moved. Commit it with the change that moved them and say
in the message why the new numbers are the right ones. A baseline bumped in its own commit,
with no explanation, is a gate that has been switched off.

## The strategy port

A strategy is anything with a name and one method
(`server/src/tindarr/swipe/strategy.py`):

```python
async def propose(self, context: StrategyContext, size: int) -> Sequence[Candidate]
```

`StrategyContext` is the whole input — taste profile, past votes, library, engagement,
cards already served, novelty, mood, media type, content filters, language, region, and a
seed for anything that would otherwise be random. A strategy reads nothing else: no
database, no clock, no global. That is what makes the replay meaningful, since the harness
can hand it the state of the world as it was at some past moment and be sure nothing from
after that moment leaked in.

`history` holds the votes cast **before** the batch being asked for, and nothing else. A
strategy is built once per user, so no cache carries one person's answers into another's
batch.

Two implementations ship today, both floors rather than candidates:

- `popular` — the most popular unvoted title. Knows nothing about taste, everything about
  fame, and duly scores 85 % already-seen on the committed fixture: the defect ADR 0013 is
  about, in one line of code.
- `random` — a seeded draw from the same pool. Nothing should ever score below it.

Adding one means writing the class, registering it in `STRATEGIES`
(`server/src/tindarr/main/evaluation.py`) with what a live run of it would cost, and
committing its baseline.
