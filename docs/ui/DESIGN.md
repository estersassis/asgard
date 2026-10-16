# ASGARD UI — design handoff (MVP 2)

This folder is the design reference for the web UI tracked in issues #19–#26. It describes what each screen shows, where its data comes from, and the visual rules to follow. It does not prescribe the implementation stack; that decision belongs to #19.

```text
docs/ui/
  DESIGN.md           this file
  screens/            one mockup per screen (*.dc.html) + support.js preview runtime
  screenshots/        PNG of each screen at 1440 px, dark theme (Setup also in light)
```

## Opening the mockups

Open any file in `screens/` in a browser. `support.js` renders the mockup format (`{{holes}}`, `<sc-for>`, `<sc-if>`, a `Component` class) so the screens work from `file://`, and the links between screens work too. Add `?theme=light` to the URL for the light palette. On `Forge.dc.html`, `?pilot=fails` shows a candidate that misses a gate.

`support.js` is a viewer for these mockups only. Do not import it, copy it, or reuse its template format in `asgard/ui`.

**All data in the mockups is illustrative.** Only pull requests #1188 and #1046 come from the real `jpadilla/pyjwt` fixtures, and the #1188 quotes are verbatim from its description. Every other PR, count, metric and confusion matrix is invented for the mockup. Never hard-code any of it.

## Principles

1. **One primary action per screen.** The orange pill button is that action. Everything else is a quiet text link.
2. **Show the minimum, reveal the rest.** Details live behind `+ More options`, `+ Details`, `why`, `Show log`. Hidden is not removed: every hidden item exists because an issue asks for it.
3. **The UI never decides.** Every number, status and verdict on screen comes from an orchestrator event or a run-directory file. No business rule lives in `asgard/ui` (#19).
4. **Same run, two doors.** A run started from the UI produces the same run directory as `asgard run`; a config saved in the UI runs unchanged with `asgard run --config` (#20).
5. **Medieval accents, modern structure.** The medieval feel comes from three things only: Fraunces serif headings (often italic), Roman numerals for stages and rounds, and the gate icon with its forge glow on the Setup screen. Layout, spacing and controls stay plain and modern. No parchment, frames, ornaments or textures, and the SECURITY.md preview looks like rendered Markdown.

## Tokens

Defined as CSS custom properties on the app root. Dark is the default theme; light comes from `data-theme="light"`. The palette extends the brand sheet in `docs/ASGARD — Marca.html`.

| Token | Dark | Light | Use |
|---|---|---|---|
| `--stone` | `#191B1F` | `#E3E1D4` | Page background |
| `--panel` | `#202327` | `#EDEBDE` | The only raised surface (SECURITY.md preview) |
| `--ink` | `#E8E6D9` | `#23261F` | Primary text |
| `--ink-soft` | `#8D9082` | `#55584C` | Labels, secondary text, quiet links |
| `--line` | `#2C2F2A` | `#C6C4B2` | Hairlines, input underlines |
| `--ember` | `#D68C42` | `#A8652A` | Brand (Forja): primary button, numerals, Classifier |
| `--ember-hi` | `#F0AC63` | `#8C551E` | Primary hover, "running" state |
| `--bifrost` | `#6FA8C7` | `#3C6E8C` | Evaluator, "under review", rate-limit waits |
| `--yes` | `#6FB981` | `#3F7A50` | Approved, security, checks passed |
| `--no` | `#D97862` | `#A8432F` | Revise, unresolved, error, failed gate |
| `--on-ember` | `#1A1408` | `#FFF8EE` | Text on the primary button |

Role colors are fixed across screens: **Classifier = ember**, **Evaluator = bifrost**. The red and green verdict colors also differ in lightness, and every verdict carries a text label or a ✓/✗ mark, so color is never the only signal.

## Type and spacing

- **Fraunces** (Google Fonts, variable): page titles 44–48 px weight 300 with the key word in italic ember; screen subheads 22–28 px weight 400–500; big numbers 32–40 px weight 300; Roman numerals.
- **JetBrains Mono**: everything else. Body 14 px / 1.6. Inputs 16 px.
- **Labels**: 11 px, uppercase, letter-spacing 0.16em, `--ink-soft`.
- **Content width**: 640 px (Setup, Pipeline), 760 px (Guideline, Forge), 1080 px (Council, two columns). Header padding 20 px 48 px. Sections are separated by 28–48 px of space or a single `--line` hairline, never by boxes.
- **Touch targets** are at least 44 px high, including text links.

## Components

| Component | Spec |
|---|---|
| Header | Gate icon + `ASGARD` wordmark (Fraunces 700, tracking 0.14em) on the left; nav on the right: `I Setup · II Pipeline · III Council · IV Guideline · V Forge`. The active item gets `--ink` and a 1 px ember underline. |
| Primary button | Pill (`border-radius: 999px`), 44–48 px high, `--ember` fill, `--on-ember` text, weight 600. One per screen. |
| Secondary button | Same pill, transparent with a `--line` border. Used sparingly (Reject, Run pilot, Regenerate). |
| Text link | No border, `--ink-soft`, 13 px, hover `--ink`. Expanders read `+ More options` / `− Fewer options`. |
| Input / select | No box: transparent background with a 1 px `--line` bottom border that turns ember on focus. The label sits above it. A validation hint (12 px) goes below: `--yes` with ✓ when OK, `--no` when not. |
| Status dot | 8 px circle in the status color; it pulses while live (classifying, under review). |
| Stage row | Roman numeral (Fraunces, ember when started), stage name, right-aligned detail. Optional note line below; a 2 px progress bar only on the running stage. |
| Tabs | Text with a 1 px ember underline on the active tab (Forge roles). |

## Screens

### I · Setup — `Setup.dc.html` · #20

Visible: Repository, GitHub token, Provider + Model, then **one credential field per provider in use**, then `+ More options`, the **Start run** button and `Load / Save asgard.toml`.

- Credentials follow every role, not only the default. If any role resolves to Ollama, show **Ollama host** (default `http://localhost:11434`, the same as `OLLAMA_HOST`). For each commercial provider in use, show its **API key** (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`). The value comes from `.env` when present and is never written to `config.json`.
- Validation hints come from the existing Stage 0 checks: the token can see the repository, and each configured endpoint answers.
- `+ More options`: Max PRs, State, Since, Until, Max iterations, Batch size (#41), Pre-filter (#7), and the model per role (Classifier, Evaluator, Synthesizer; `default` inherits `[llm.default]`).
- Maps 1:1 to `RunConfig` / `asgard.toml`.

### II · Pipeline — `Pipeline.dc.html` · #21

Eight stage rows in pipeline order: mine, normalize, prefilter, classify, consolidate, synthesize, security_md, bundle.

| On screen | Source |
|---|---|
| Stage state and detail | `StageStarted(stage, total)`, `StageFinished(stage, skipped)` |
| Mined count | `PRMined` |
| Classify progress `48 / 58` | `ClassifyProgress(kind, index, total)` |
| "Waited 41 s for the GitHub rate limit" note under Mine | `RateLimited(wait_seconds)` |
| `58 kept · 142 dropped` | `PRFiltered` count and `prefilter/removed.json` |
| Error line + `Retry PR` | `Error(message, stage)` |
| `Show log` | tail of `events.jsonl` |

Controls: **Stop**, after which the primary button becomes **Resume from `<stage>`**, the equivalent of `asgard run --config … --from <stage>`. There is no pause. Acceptance (#21): the progress matches `events.jsonl` of the same run.

### III · Council — `Council.dc.html` · #22

The demo centerpiece: agents disagree, then converge.

- **Totals**: security / not security / unresolved. One line below: approval at round I → final, and mean rounds, from `classify/metrics.json` (`approval_rate_iteration_1`, `approval_rate_final`, `mean_iterations`).
- **PR list**: dot + `#id` + title + short status. The statuses are classifying (after `ClassificationAttempt`), review (waiting for `EvaluatorVerdict`), security or not security (approved), unresolved (`unresolved_max_iter` / `unresolved_no_progress`), and error.
- **Timeline of the selected PR**, one block per round (Roman numeral):
  - *Classifier*: the decision plus the first evidence quote, in Fraunces italic.
  - *Evaluator*, indented with a bifrost rule: the verdict (`APPROVE` / `REVISE`); the four findings `decision · evidence · completeness · rationale` as ✓/✗ on one line, with `why` revealing each `explanation`; and the `feedback` text.
- Source: `classify/trace.jsonl`. Acceptance (#22): any PR's timeline matches its trace lines.

### IV · Guideline — `Guideline.dc.html` · #23

- The SECURITY.md is rendered as plain Markdown on `--panel`: H1, H2 "User Guideline", topics as H3 with bullet recommendations, citations `(#123)`, and the provenance footer exactly as `security_md` writes it.
- Clicking a citation expands the evidence quotes (from `consolidate/output.json`) and an `Open #123 on GitHub` link.
- A citation outside the approved set is shown in `--no` (`unknown_citations` / `CitationWarning`).
- `Edit prompt` opens the synthesis prompt; **Regenerate** reruns `--from synthesize` and must not call the Classifier or Evaluator (#23 acceptance).
- Downloads: **SECURITY.md** (primary) and `Run bundle (.zip)`.

### V · Forge — `Forge.dc.html` · #24, #25, #26

The advanced mode. It is not part of the normal run flow.

- Role tabs (Classifier · Evaluator · Synthesizer) → **Version** select (name, sha256, date, note) with `Save as new version` (#24) → prompt editor.
- **Pilot set** select (the shipped Django VEM 2026 example, or upload one) and **Run pilot** (#25).
- Result: F1 and Cohen's κ, active → candidate, with their gates. `+ All metrics and gates` reveals accuracy, precision, recall, F1 and κ (active vs. candidate, with editable gates), the 2×2 confusion matrix, and single pass vs. full loop F1.
- **Promote** is disabled when any gate fails (#25 acceptance); **Reject** keeps the active prompt.

## Not covered by the mockups

- **Run history.** The screens assume one run, and no issue covers opening a previous run yet.
- **#19 stack decision.** The mockups assume a web UI (it works better on a projector), but nothing here depends on a specific framework.
- **Narrow screens.** The layouts wrap but were only designed at 1440 px; the demo target is a laptop or projector.
