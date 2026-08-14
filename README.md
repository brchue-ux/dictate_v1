# dictate_v1

[![CI](https://github.com/brchue-ux/dictate_v1/actions/workflows/ci.yml/badge.svg)](https://github.com/brchue-ux/dictate_v1/actions/workflows/ci.yml)
— that badge means *it compiles on Windows and the tests pass there*. It is not a
GPU check; the machines that run it have no graphics card. See
[what was verified, and what was not](#what-was-verified-and-what-was-not).

Push-to-talk dictation for Windows. Hold a hotkey and speak — the words appear on
screen **while you are still talking**. Let go, and a second or two later clean,
punctuated text is pasted into whatever window you were in.

Everything runs on your own machine. Nothing is sent anywhere, and there are no
per-use charges.

```
   hold hotkey ──► mic ──► the whole take, kept in memory
                     │
                     ├──► live captions on screen  (fast, rough, DISPLAY ONLY —
                     │    they never reach your document)
                     │
   release ──────────┴──► Whisper large-v3-turbo on your GPU
                                ▼         (the captions stay on screen, greyed,
                          strip the "um"s  while this runs)
                                ▼
                          paste into the window you were in
                                ▼
                          the captions go, the panel says "pasted"
```

The captions and the pasted text come from two different models, on purpose. The
caption model is fast enough to keep up with your voice but writes in lower case
with no punctuation and gets the odd word wrong. That is fine, because **caption
text never reaches your document** — the text that gets pasted comes from Whisper
reading the whole recording after you stop.

*(The caption model changed on 2026-08-13. If your config predates that, see
"The caption model changed" below — all four `[captions]` file settings have to
move together.)*

---

## Setup

**Double-click `setup.cmd`.**

That is the whole thing. Windows will ask for permission to install software —
click **Yes** — and then, before it starts, one question: whether dictate should
start by itself when you log in. Answer it (or do not — thirty seconds of silence
means no, and it carries on either way), and the rest runs by itself for about
half an hour, mostly downloading. There is nothing to edit afterwards.

If you would rather see it in a window you started yourself, this is the same
thing:

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

### What it is doing, and roughly how long each part takes

| | | |
|---|---|---|
| 1 | **Checks this PC** — Windows version, your graphics driver, free disk space | seconds |
| 2 | **Installs the build tools** — Python, Git, CMake, the Vulkan SDK, the C++ compiler. Anything already installed is left alone | 5–15 min |
| 3 | **Builds whisper.cpp** with the Vulkan backend, from source | 5–15 min |
| 4 | **Downloads the two models** (1.7 GB) | 5–20 min |
| 5 | **Installs dictate** and writes a config file with every path already filled in | 1 min |
| 6 | **Checks it actually works** — can Vulkan see your card, does the transcriber start, does a test clip come back as the right words | 1–2 min |

It needs about 25 GB of free space and, at the end, prints the one thing you
type to start dictating:

```powershell
dictate run
```

Hold **Ctrl + Alt + Space**, speak, let go. Ctrl+C in that window to quit.

### Or have it start by itself when you log in

**Setup asks you.** Right at the start of the run — while you are still at the
keyboard, not half an hour later — it asks whether dictate should start when you
log in, and carries the answer out at the end once everything has been checked.
No answer within thirty seconds, or a scripted run with nobody there, means no
and nothing is registered. `-Autostart yes` (or `no`) answers it up front and
skips the question.

You can also settle it at any time afterwards, in either of two places:

* the right-click menu of the dictate icon by the clock — **Start when I log
  in**, ticked when it is on, one click either way;
* the command, which is what that menu item names:

```powershell
dictate autostart enable
```

**Turning it on starts it there and then.** You do not have to log out and back
in to see whether it worked: whichever of the three ways you used, dictate
starts the same windowless copy the logon task would, and says which of three
things happened — it started (with its process number), a copy was already
running so nothing new was started, or it could not start and here is what
stopped it. That last one never appears under a sentence saying everything
worked; the logon task's own outcome is reported separately from it.

From then on it is just there: about half a minute after you reach the desktop,
without a window appearing, ready for the hotkey. You never type anything to
start dictating again.

```powershell
dictate autostart status        # is it on, is it running, and did it start?
dictate autostart status --why  # ...and what is actually running, if those disagree
dictate stop                    # stop the copy that is running, now
dictate autostart disable       # never mind, go back to how it was
```

**What it costs.** Almost nothing while you are not dictating. The 1.6 GB of
graphics memory is handed back after five idle minutes either way — see [your
graphics card is only busy while you are
dictating](#your-graphics-card-is-only-busy-while-you-are-dictating) — so
leaving this on is not the same as leaving that memory tied up all day, and
there is no reason to turn it off before a game. What stays is one background
Python process, a few tens of megabytes of ordinary memory, waiting for a
keypress.

The one case where it does cost you the card: if you have set
`idle_release_minutes = 0` to keep the model loaded permanently, then starting
at logon means it is loaded from logon. Then it *is* 1.6 GB all day, and
`dictate autostart disable` — or setting that back to 5 — is the remedy.

Two other things worth knowing:

* **Only one copy ever runs.** If autostart already has it and you type
  `dictate run`, the second one tells you which copy is already running and
  stops, rather than quietly fighting it for the hotkey.
* **If it cannot start at logon, it says so.** It waits and tries again a few
  times first — at logon the graphics driver may still be loading — and if it
  still cannot, it puts a message on screen and writes the reason to
  `%LOCALAPPDATA%\dictate\autostart.log`. `dictate autostart status` prints that
  reason back to you. Nothing disappears into a window that closed.

  That message box is modal, so the process it belongs to stays alive until you
  close it — and Windows goes on counting the logon task as *running* for
  exactly that long, while dictate is not running at all. `dictate autostart
  status` says so in as many words when it finds that state, rather than
  reading "the task is running" as "dictate is running". Closing the box is
  what lets that process end.

Turning it off is one command — or the same tray item, unticked — and leaves
nothing behind: it is a single Windows scheduled task, which you can also see
and delete in Task Scheduler under the name `dictate`.

**Turning it off does not stop the copy that is running.** That is deliberate:
you could be in the middle of a sentence when you untick it, and the answer to
"should this start at the next logon" is not a reason to throw away what you
were saying. It says so, names the copy, and names the command — `dictate stop`
— that ends it when you actually want it gone.

All three places answer this question by asking Windows the same way, so setup,
the tray tick and `dictate autostart status` cannot disagree. The tray reads it
afresh every half minute, so a `dictate autostart enable` typed in a window
shows up on the menu a moment later rather than at the next restart.

### If setup stops part way

It is meant to. Setup never carries on past something that went wrong, and the
last thing on screen is always the problem in one sentence and what to do about
it. Do that thing, then **run setup again** — it keeps everything it has already
done and carries on from where it stopped. A download that died at 90% resumes
at 90%; a compile that finished does not happen twice.

Two of them are worth knowing in advance:

* **"Restart the PC and run setup again."** Some installers only become visible
  to Windows after a restart. This is not setup being cautious — it genuinely
  cannot see the tool until you do.
* **"This window does not have administrator rights."** Close it, right-click
  **Windows PowerShell**, choose **Run as administrator**, and run it again.
  (Double-clicking `setup.cmd` asks for this on your behalf.)

The full technical detail of every run, including the compiler's own output,
goes to `C:\dictate-gpu\setup-log.txt`. Nothing you need is only on screen.

### Re-running one part on its own

```powershell
# Just check everything again - useful after a driver update
powershell -ExecutionPolicy Bypass -File setup.ps1 -Only verify

# Rebuild whisper.cpp at a known-good version, if a new one ever misbehaves
powershell -ExecutionPolicy Bypass -File setup.ps1 -Only build,verify -Ref v1.9.2 -Rebuild
```

`scripts\build-whisper-vulkan.ps1` and `scripts\fetch-models.ps1` still work and
still do what their names say; they now run the same code as the step of setup
that does that job, rather than a second copy of it.

### Getting a newer version

```
dictate update
```

Seconds, not the half hour the first install took. It fetches the current source,
tells you what changed in the words of the changes themselves, puts it in place,
and starts dictate again if it was running:

```
install folder:  C:\dictate_v1
                 (this is the folder Python loads dictate from)
this copy:       0f6ae00 on main, recorded 2026-08-12 22:31:04
latest on main:  bd4d941  Make an orphaned whisper-server impossible

What changed:
  - Make an orphaned whisper-server impossible, and give him one command out
    of any stuck state
  - tidy: the tray leaves nothing half-alive when it cannot start

Fetching the new version…
29 file(s) to update.
```

**It does not rebuild anything.** The build tools, the compiled `whisper.cpp` and
the models live in `C:\dictate-gpu` and do not change when the application does,
so none of them are touched — that is the whole reason this takes seconds. If a
future change ever does need one of them, it will say so and name the exact
`setup.ps1 -Only <step>` to run. Your settings in `%APPDATA%\dictate` are never
opened either.

**The first time, it will ask you to sign in to GitHub.** The repository is
public, so the sign-in is not about being let in: the GitHub CLI asks GitHub for
things through its API, and it will not make an API call at all without one. So:

```powershell
winget install --id GitHub.cli     # if you do not have it
gh auth login                      # opens your browser; once, ever
```

Choose **GitHub.com**, then **HTTPS**, then **Login with a web browser**. Windows
remembers it from then on, and `dictate update` never asks again. dictate itself
never sees, prints or stores the credential — the GitHub CLI holds it, in the
same place Windows keeps your other sign-ins.

Two things worth knowing:

* `dictate update --check` says what would change and changes nothing.
* `dictate update --restore` puts back the version that was there before the last
  update. A complete copy of your install folder is kept every time anything is
  changed, at `%LOCALAPPDATA%\dictate\updates`, so there is always a way back.

If you have edited a file inside `C:\dictate_v1` yourself, it stops and names the
file rather than overwriting it. `dictate update --force` goes ahead anyway, and
still keeps the copy.

Both of these are on the icon by the clock as well, as **Check for updates** and
**Update now**, so you never have to open a window to find out whether there is
anything new. Choosing one of them **opens a window of its own** and runs the
command in it — the same output, the same questions answered, and it stays open
until you press Enter, so a failure is still on screen afterwards. **Update now**
then stops this copy, replaces the files and starts it again, which means the
icon disappears for a few seconds part way through and comes back on the new
version. The window is what is on screen in between.

### If anything is ever stuck

```
dictate stop
```

That is the whole answer, and it is the only command worth remembering. It does
not matter what went wrong — dictate will not start, a window was closed with
the X, something says the transcription port is already in use, a copy is
running that you cannot see. `dictate stop` looks at each thing dictate can
leave behind and clears whichever of them is there:

- a copy that is running is asked to shut down, exactly the way Ctrl+C asks;
- one that will not answer is ended, **together with the whisper-server it
  owns** — never the parent on its own, because that is what strands a
  transcription process holding your graphics card's memory;
- a whisper-server left over from an earlier run is ended too;
- and if something that is *not* dictate is holding the port, it says which
  program, and prints the exact command to type. It never ends anything that is
  not ours.

You can also right-click **the dictate icon by the clock** — it is there
whenever dictate is running, including when it started by itself at logon and
there is no window anywhere. It shows what dictate is doing (grey while it
waits, gold while it is listening to you, red if something needs reading), and
carries **Caption size**, **Stop**, **Restart**, **Check for updates**, **Update
now**, **Change the hotkey**, **Start when I log in** — ticked when it is on,
one click either way — and **Open the log folder**. Each one is labelled with the command
that does the same thing, so the icon teaches you the commands rather than
replacing them. Turn it off with `[tray] enabled = false` if you would rather
not have it.

### If something is wrong later

`dictate doctor` first. It checks the operating system, Python, the hotkey, the
cleanup rules, the whisper-server binary, the model file (including whether the
download was truncated), the port, the caption model, the Python packages and
your microphones — and puts what to do about each failure at the bottom.

If it is set to start when you log in and did not, run `dictate autostart status`
as well: it says whether Windows ran it, what it returned, whether the process
it started is still there, and what went wrong the last time it tried. When
those readings do not add up — the task counted as running while the copy in
front of you was started by hand — it says so instead of picking one, and
`dictate autostart status --why` is the one line that lists what is actually
running: the process the logon task started, whether it is still alive, any
whisper-server, and who holds the transcription port. It reports and changes
nothing, so it is safe to run at any time, including mid-sentence.

The full log, including whisper.cpp's own output, goes to the file named under
`[logging] file` in your config. The separate, much shorter record of what
happened at logon is at `%LOCALAPPDATA%\dictate\autostart.log` — separate on
purpose, because a config file that will not load is one of the things that can
go wrong at logon, and the report of that cannot live somewhere the config
points to.

---

## Configuration

One file, `dictate.toml`, with a sane default for everything —
`config/dictate.example.toml` is the annotated copy of it. Setup writes it for
you at `%APPDATA%\dictate\dictate.toml` with the paths already pointing at what
it installed, so there is nothing you have to fill in.

It is yours to edit after that. Setup will not overwrite your version: running
it again only re-points those paths, and leaves every other line — the hotkey,
the overlay colours, anything you have changed — exactly as you left it.

A typo in that file is an error with a message, not a silently ignored setting.

### Cleanup rules

`cleanup-rules.toml`, next to the config, is yours to edit — filler words, filler
phrases, and guarded patterns. Save it and the next thing you dictate uses the
new rules; a broken edit leaves the previous rules running and tells you what is
wrong with the new ones.

**These rules can only ever delete words.** There is no "replace with" option
anywhere in the file, and after the rules run, dictate checks that the words it
is about to paste are a subset of the words Whisper actually heard, in the same
order. If a rule ever breaks that, the whole cleanup is thrown away and Whisper's
text is pasted exactly as it came out. So the worst a mistake in there can do is
remove something you wanted to keep.

That is also the one thing the check cannot catch, because deleting is what the
pass is for: a rule that eats a phrase you actually said is, to the check, a rule
working correctly. So the rules for phrases that are sometimes real speech —
"you know", "I mean", "sort of", "kind of", "like I said", "if that makes sense"
— require the commas Whisper writes around a phrase when it hears it as filler.
"It is, you know, mostly fine" loses it; "Do you know the answer?" keeps it. If
you add a phrase of your own to `filler_phrases`, it is removed *everywhere*,
with no such guard — the comments in the file say when that is safe.

Try a rule without dictating:

```powershell
dictate clean --explain "Um, so I was, you know, thinking about it."
```

### Spoken punctuation

Say "hello comma world" and get `hello, world`. **Off until you turn it on:**

```toml
[punctuation]
enabled = true
```

`voice-punctuation.toml`, next to the config, is the list of what you can say —
period / full stop, comma, question mark, exclamation mark / point, colon,
semicolon, dash, open and close quote, open and close bracket, new line, new
paragraph. It is yours to edit like the cleanup rules are, and adding "new
section" is one block of four lines.

It is off by default for one reason: every phrase in that file is a phrase you
can then no longer dictate literally, and a new version of dictate is not
allowed to start eating the word "period" out of sentences you are dictating
today. With `enabled = false` the pasted text is byte for byte what it was
before this existed.

**When is "comma" a mark and when is it the word?** The rule is one line: it is
the WORD when the word in front of it is a determiner or possessive — "the
comma goes here", "set the period to five minutes", "put it on a new line" —
and the MARK everywhere else. Plurals are never touched at all. It is a blunt
rule and it is sometimes wrong; when it is, say **"literal"** in front of the
phrase:

```
"I am not going literal full stop"   ->   I am not going full stop
```

**This stage substitutes, so it is not part of the cleanup pass** and the
deletion-only guarantee above is untouched. It carries a narrower one of its
own: a rule may only insert punctuation and whitespace — dictate refuses to load
the file if a rule would insert a letter or a digit — and afterwards the same
subsequence check runs again. So this cannot put a word you did not say into
your document either. Every substitution it makes goes into the log with the
words that produced it, so a mark in the wrong place can be traced instead of
being a mystery.

One warning about `new line` and `new paragraph`: a line break is delivered as a
Return keypress, because that is the only thing that puts a new line into
another application. **In a chat box or a terminal, Return submits.** Delete
those two blocks from the file if that is where you dictate.

Try it without dictating:

```powershell
dictate punctuate --explain "hello comma world"
```

### The caption model changed

**On 2026-08-13.** The old one was trained on read audiobooks and got whole
phrases wrong on ordinary speech: on the bundled test clip it printed *"AND SAW
MY FELLOW AMERICANS ASK NOT WHAT'S YOUR COUNTRY CAN DO FOR YOU AS BUT YOU CAN DO
FOR YOUR COUNT"*. What is there now got the same clip right, and stayed right
with noise added down to 5 dB. It also has the first word on screen about 0.2 s
sooner and is not a word behind when you let go, which the old one always was.

If your config was written before that date, this fetches the new model and
re-points all four `[captions]` settings at it — the file names inside the folder
changed with the model, so the folder alone is not enough:

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1 -Only models,install
```

The old folder is left where it is; delete it once you are happy. If you skip
this, dictate starts with live captions off and says which files it could not
find — the pasted text is unaffected either way.

**To judge it on your own voice**, which is the only judgement that counts:

```powershell
dictate captions some.wav
```

It uses your config, so it measures the model you are actually running, and it
prints when the first words appeared, how often they changed after that, what
the model finally heard, and whether it kept up with the speech at all. Point
`[captions]` at a different model and run it again on the same file to compare.
`--timeline` prints every change with how far into the recording it happened.

### The caption window

The words that appear while you are speaking come up in a panel at the bottom of
the screen. To look at it without dictating — it shows sample text and runs the
whole appear, fill, hold, clear and fade cycle:

```powershell
dictate overlay
```

`dictate overlay --error` shows the failure state, `--repeat 3` loops it, and
`--rate 150` runs the words in faster than real dictation does.

#### Making it bigger or smaller, or changing the font

Three things are worth changing, and none of them needs you to open a file or to
work out a pixel measurement:

| You want | Type this |
|---|---|
| the whole panel a bit smaller | `dictate look smaller` |
| the whole panel a set size | `dictate look medium` |
| **just the words** bigger or smaller | `dictate look --text bigger` |
| **just the panel** bigger or smaller | `dictate look --panel small` |
| a different font | `dictate look --font "Consolas"` |
| to see what fonts you have | `dictate look --fonts` |
| to pick a size with the mouse | the tray icon → **Caption size** |
| to *try* any of those before keeping it | `dictate overlay --size small` |

The sizes are `small`, `compact`, `medium`, `large` and `huge`; **it ships at
`compact`**, and `huge` is the panel exactly as it was before this existed. Each
step is about three pixels of caption text — 12, 15, 18, 21, 24 at 100% display
scaling.

`dictate overlay --size small` (or `--text`, `--panel`, `--font`) shows you the
result and then asks whether to keep it, so you can look at three sizes in half a
minute and never type a number. Answering `y` writes the one line into your
config; `--keep` skips the question. `dictate look` on its own says what the
captions are now and what is a step either side.

The **one knob is `size`** and it is the one to use: it moves the words, the
width, the padding and the shoulder together, so the panel looks deliberate at
every setting rather than like a small line of text in a large box. `--text` and
`--panel` split that apart when you want them split — a smaller box will still
grow enough to fit the words you asked for, because text vanishing off the
bottom of the panel while you speak is worse than a box that is wider than you
asked for.

The notification-area icon has a **Caption size** submenu too — the five sizes,
with a tick against the one you are on — and choosing one takes effect on the
very next thing you say. It sits beside **Change the hotkey**, works the same
way, and writes to your config the same way. Everything else about the
panel — the colours, the position, the fade — is still the `[overlay]` block of
your config; change a value there and run `dictate overlay` to see it.

**What it shows, at each moment:**

| While you | The panel says | And shows |
|---|---|---|
| hold the hotkey | `listening`, in gold | your words, arriving as you speak |
| have let go, and it is transcribing | `thinking`, in grey | *the same words*, now grey too |
| have had the text pasted | `pasted`, in grey | nothing — the words go as the text lands |
| are looking at a failure | `error`, in red | what went wrong, for six seconds |

The words staying up through the middle row is the point: that is the second or
so where the graphics card is working and there used to be an empty panel. They
go grey the moment you let go — that is how you can tell it registered — and
they are still the caption model's lower case, so they can never be mistaken for
the text that is about to be pasted. What ends them is the text actually
arriving in your document.

Three other things about it are worth knowing:

* **It appears on the screen you are working on.** Not always the primary one —
  it uses the monitor holding the window you were in when you pressed the
  hotkey, which is the window your text is about to be pasted into. If that
  cannot be worked out it falls back to the monitor your mouse is on, then the
  primary. `follow_focus = false` restores the old always-primary behaviour.
* **It is sized for the monitor it lands on.** Every pixel measurement in
  `[overlay]` is at 100% scaling and is multiplied by that display's own
  scaling, so a second screen at 150% gets captions the same physical size
  rather than two thirds the size. If that misbehaves, `dpi_awareness = "off"`
  hands the scaling back to Windows. `size` sits on top of that rather than
  instead of it: a `compact` panel on a 150% screen is 150% of a compact
  panel.
* **It is set in Fira Code**, which is already installed on this PC. If it ever
  is not, dictate uses Consolas and **says so when it starts** — it will not
  quietly draw a different font and leave you wondering.

It never takes focus and clicks pass straight through it, which is what stops
your text being pasted into the wrong window. That has not changed and is not
allowed to.

### Your graphics card is only busy while you are dictating

The transcription model takes about **1.6 GB** of your card's memory. It used to
sit there for as long as dictate was running — all day, whether or not you had
said anything since breakfast — which is memory a game or a compute job could
have had.

Now dictate hands it back after **five minutes without dictating**. Between
dictation sessions the card is genuinely idle as far as dictate is concerned:
the full 16 GB is available to everything else, and there is nothing to close or
remember to close.

You get it back by dictating, and you are unlikely to notice it happening.
Loading the model starts the moment you **press** the hotkey — not when you let
go — so it happens while you are still speaking, which is about how long it
takes. The words on screen while you talk come from a different, much smaller
model that runs on the processor, so they appear exactly as usual either way.
Worst case, on a short sentence after a long gap, the paste is a second or two
later than normal, once.

To turn it off and keep the model loaded permanently, set this in your config:

```toml
[whisper]
idle_release_minutes = 0
```

`dictate doctor` tells you what it is set to and whether a model is loaded at
this moment.

### What you have dictated, kept so you can look back over it

Every dictation that produced words is written to a plain text file, newest
first — including one dictate refused to paste, which is marked as such and is
how you get those words back. To read it — or to get rid of it:

```powershell
dictate history            # opens it
dictate history --delete   # deletes the whole thing, there and then
```

Both are on the icon by the clock too, which is where to find them when dictate
started itself at logon and there is no window to type in.

It lives beside your config, as `%APPDATA%\dictate\history.txt`. Each entry is:

* **the text that was pasted**,
* **when you said it, and how long you spoke for**,
* and **what Whisper heard before dictate changed anything** — but only when it
  did. Two things can: the cleanup rules, which delete, and spoken punctuation,
  which substitutes. So that line always means "look, this is what dictate did
  to it" rather than repeating the same sentence twice.

It does not record how long transcription took, which window the text went to,
or a dictation that produced no words at all. Every line in it is text you said;
a line that says it was not pasted is there precisely because that is the only
copy of it that outlives your clipboard.

**It is bounded.** The last **200** dictations, with the older ones dropping off
the end. Change that, or turn the whole thing off, in your config:

```toml
[history]
enabled = true
keep = 200
```

It is yours and it stays on this machine: nothing about it is sent anywhere,
nothing else reads it, and there is no code in dictate that could. Turning it
off stops anything new being written — a file that is already there is left
alone, deliberately, so delete it first if you want it gone.

### If you click somewhere else while you are still speaking

dictate reads which window to paste into when you **press** the hotkey, not when
your words are ready — otherwise the caption panel appearing could move the
paste. So if you click away mid-sentence and are still somewhere else when the
transcription comes back, there is a decision to make, and dictate makes the
careful one:

**It pastes nowhere, and tells you where your words are.** They go on your
clipboard — one Ctrl+V puts them wherever you want — and into your dictation
history. Nothing is ever typed into a window you did not dictate into, and no
window is dragged in front of what you are doing.

Clicking away and clicking **back** before the words arrive is not a change at
all: that pastes as usual. So does a notification stealing focus for a moment.

If you would rather it brought the window you started in back to the front and
pasted there — which is what dictate used to do, and is the right answer if you
dictate long passages into a document and read something else while they
transcribe:

```toml
[paste]
on_focus_change = "restore"
```

The same thing happens if a paste fails for any other reason (an application
running as administrator will refuse synthesised keystrokes): the words are kept
and you are told, rather than lost. `hold_to_clipboard = false` keeps your
clipboard out of it and leaves the words in the history only.

### Your clipboard is not touched

The default paste method synthesises the characters as keystrokes and never uses
the clipboard at all. (`[paste] method = "clipboard"` is available for the few
apps that mishandle long keystroke runs; that mode saves and restores your
clipboard, and refuses to clobber contents it cannot faithfully put back.)

The one exception is above: when dictate could **not** paste, it puts the words
on your clipboard so you can place them yourself, and does not put the previous
contents back. It says so at the time, and it is the only case where losing a
clipboard is better than the alternative — which is losing what you just said.

### What is pasted cannot press Enter

**A line break is pasted as a space, not as a Return.** Return has to be a real
keypress — it is the only way to put a line into another application — and in a
terminal, a chat box or a search field a Return *submits*: it runs your command
line, or sends the half-written message. Dictation is not allowed to do that on
its own.

Whisper's own output is what made this matter: `whisper-server` returns one line
per segment, so any dictation it split in two arrived with a newline in the
middle of it and pasted an Enter into the middle of a command. That is fixed
where it comes from, and this is the wall behind it.

If you dictate into a document and you want line breaks — including the "new
line" and "new paragraph" spoken marks — turn them on deliberately:

```toml
[paste]
line_breaks = "return"
```

Nothing else brings a Return back. And whichever you choose, it is written down:
an entry in [what you have dictated](#what-you-have-dictated-kept-so-you-can-look-back-over-it)
says when pasting it pressed Return.

The same setting's neighbour, `modifier_wait_ms`, covers the other way a paste
can become a command: if you are still holding Ctrl or Alt when the text is
ready, dictate waits a moment for you to let go rather than typing `the` into
the window as Ctrl+T, Ctrl+H, Ctrl+E.

### Changing the hotkey

Right-click the dictate icon by the clock → **Change the hotkey**, and pick one.
It takes effect immediately and is written into your `dictate.toml`, so it is
still there tomorrow. If Windows will not give dictate that combination —
usually because another program already owns it — the old one stays and nothing
is written.

The same thing typed, for a combination that is not on the menu:

```powershell
dictate hotkey                        # what it is now, and what the tray offers
dictate hotkey "ctrl + alt + k"       # set it (a running copy keeps the old one
                                      #  until you restart it, and says so)
```

### Or one mouse button, held the same way

Three of them are on the same menu, and in the same config setting:

```toml
[hotkey]
combination = "mouse 4"               # or "mouse 5", or "middle mouse button"
```

Hold it, speak, let go — exactly as the chord works. Left and right are refused:
holding a button to talk means dictate has to swallow it, and a machine whose
left button does nothing cannot be used, including to turn dictate off again.

**The button keeps its own job, on a click.** dictate swallows the button as it
goes down — so Back does *not* fire the instant you start talking — and then
looks at how long you held it. Longer than `[audio] min_utterance_ms` and it was
a dictation; the button never reaches the window at all. Shorter and it was a
click, so dictate sends the click on to the window you clicked in, a millisecond
or two after you let go rather than as you pressed. Set
`[hotkey] mouse_click_through = false` and the button belongs to dictate alone
while dictate is running.

One thing you will see either way: **a click flashes the caption panel** for as
long as the click lasts. Pressing the button starts a recording, and a recording
shorter than `min_utterance_ms` is thrown away — the same path a tapped hotkey
already takes. Deferring the start to avoid it would mean throwing away the
first fraction of a second of everything you say, which is the worse trade.

**What each button costs**, because push-to-talk means holding a button down for
seconds at a time and that is not the same question as what a click of it does:

| Button | A click of it is | Held down, it normally |
|---|---|---|
| **Mouse 4** (recommended) | Back | nothing — no common application does anything with it held |
| **Mouse 5** | Forward | nothing, same as Mouse 4 |
| **Middle** (the wheel) | open a link in a new tab, close a tab | **starts autoscroll** — the scrolling cursor — in browsers and Explorer, and pans in map, drawing and PDF applications |

Mouse 4 is the one to pick. Nothing anywhere does anything with it *held*, which
is the whole of what push-to-talk asks of a button, and what a click of it does
— Back — is one keystroke to undo if a click ever does go astray. The middle
button is a materially worse fit: dictate swallows it while dictate is running,
so autoscroll does not start, but anywhere dictate's hook does not reach (a
window running as administrator, a game reading the mouse directly) holding it
starts autoscroll in the middle of your sentence — on top of losing new-tab and
close-tab to the click-through path. Mouse 5 costs the least of the three;
Forward is the least-used button on a mouse.

**The keyboard chord keeps working.** A mouse button is visible to dictate only
through a low-level mouse hook — Windows tells applications about hotkeys, and
about mouse buttons only that way — and a hook is something Windows can refuse
and security software can remove. So `[hotkey] keyboard_fallback` (Ctrl + Alt +
Space unless you change it) is registered alongside it and always works. If the
hook is refused or lost, dictate says so, turns its icon red, and carries on with
the chord; it never sits there looking fine while the button does nothing.

`dictate hotkey` prints all of the above for whichever button you are on, and
`dictate doctor` reports the trigger you actually have.

---

## What was verified, and what was not

This was built on Linux. **There was no Windows machine and no AMD GPU
available.** Since then a Windows CI build has been added, which moves several
things out of "written carefully, never run" and into "run on real Windows". The
GPU is not one of them, and cannot be: GitHub's Windows machines have no
graphics card in them at all.

So there are now three lists, not two.

### Verified anywhere — 984 tests, run and passing

```bash
python -m unittest discover -s tests -t .
```

* The whole dictation state machine end to end: press → audio → captions →
  release → transcribe → clean → paste, plus every error path through it
  (transcription failure, paste failure, unexpected exception, captions failing
  to start, captions crashing mid-sentence, an unreadable focused window, an
  overlong utterance, a tap of the hotkey, silence, overlapping utterances).
* **That caption text can never be pasted, whatever is on screen and whenever.**
  It is not held anywhere in the pipeline, the value handed to the paste worker
  has no field a string could travel in, the injector is called from exactly one
  place, and the decoder that produced the words is shut on release. Including
  the case the new timing creates: the caption still up, in front of you, at the
  moment the real text is delivered — and still not what is delivered.
* **That the words stay up until the text lands, and not a moment longer**: on
  screen while the graphics card works, gone as the text arrives, gone
  immediately on a mis-press, and replaced by the message when something fails.
  Nothing carries over from one dictation to the next.
* **What the dictation history keeps, and what it will not do**: newest first,
  the raw text only when the cleanup rules changed something, capped so the file
  cannot grow without limit, written atomically, deleted completely, nothing at
  all written when it is off, and a disk that will not take it costing one
  message rather than the dictation.
* That the target window is captured at press and not at paste time.
* **What happens when you click somewhere else while you are still speaking**:
  that nothing is pasted anywhere, that the words are on the clipboard and in
  the history marked as not pasted, that both windows are named in what you are
  told, that clicking away and back again pastes as usual, that a window which
  closes mid-sentence is a different message from one you moved away from, that
  `on_focus_change = "restore"` puts the old behaviour back and says out loud
  that it moved a window, that there is **no** setting which pastes into
  whatever you happen to be looking at, and that a paste which fails for any
  other reason keeps the text the same way instead of discarding it. Also that a
  clipboard which refuses, or a history that is switched off, changes what you
  are told rather than being claimed anyway — and that none of it leaves the
  held text sitting on the pipeline.
* The resident-backend lifecycle against a **real child process**: start, wait
  for health, slow start, crash → restart, repeated crashes → give up with a
  reason, clean shutdown, and the stop-during-restart deadlock. Including that
  after an idle release the server really can be started again on the same port,
  and that the restart-on-crash supervision applies to that second process too.
* **When the graphics card's memory is handed back and taken again**: the idle
  timer, that a running transcription is never unloaded from under itself, that
  the load starts at hotkey press rather than at release, and every awkward
  collision — pressing while the shutdown is running, pressing again while a
  load is already running, an utterance that ends before the model has loaded,
  two overlapping utterances, and shutdown arriving in the middle of any of it.
  Also that a model which cannot be loaded again says so and never silently
  pastes nothing.
* The whisper-server HTTP client against a **real HTTP server**: multipart WAV
  upload, response parsing, HTTP errors, nothing-listening.
* The cleanup pass, including the "can only delete" guarantee — and that every
  shipped rule preserves it on realistic dictation.
* **Spoken punctuation**, against transcripts that really came out of Whisper
  large-v3-turbo rather than invented examples: every mark it can produce, the
  spacing and capitalisation around each one, that ordinary speech comes back
  byte-identical, that the phrases which are also real words survive, the
  escape, the "off gives you exactly today's behaviour" path, and the cases the
  mark/word rule gets **wrong**, asserted on purpose so that fixing one is a
  visible change. Also that the cleanup pass's own guarantee is exactly where it
  was: same schema, same subsequence check, and still handed Whisper's text
  byte for byte.
* Config and rules validation, WAV encoding, the utterance buffer, UTF-16
  surrogate handling for text injection, hotkey parsing, and the doctor's
  reporting.
* **Everything about the caption overlay that is not the window itself**: the
  fade curve and its mid-flight reversal, the font fallback decision, and the
  per-monitor layout arithmetic — including that a second monitor at 150%
  scaling gets a *bigger* caption rather than a smaller one, that the panel
  lands inside that monitor's work area and not the primary one's, and that it
  still fits on a 1366×768 laptop. Plus the look's own rules, checked against
  the source: no geometry call in the caption path, no call anywhere in the
  file that could activate the window, and comfortable rather than maximum
  text contrast.
* **That only one copy can run**, with a second real process contending for the
  lock — including that a copy killed outright leaves no lock behind, and that
  the second one is told which copy is already running rather than failing
  obscurely.
* **What the logon task says** — that it triggers on this user's logon, runs in
  his own session without administrator rights, has no execution time limit, and
  starts the interpreter that has no console window.
* **That a logon start that fails writes down why**, retries a bounded number of
  times first, does not retry a broken config file at all, and stands aside when
  a copy is already running.
* **That turning it on starts it now as well** — the same windowless, detached
  copy the logon task starts, never a second one on top of a copy that is
  already running (against a real instance lock, held here), and that what is
  reported is only what was watched happen: a copy that came straight back is a
  failure carrying its exit code, running out of patience is "not confirmed"
  rather than either answer, and a start that failed is never printed under the
  sentence saying the task registered. And that turning it off leaves a running
  copy alone, names it, and names `dictate stop`.
* **That `dictate stop` gets out of every stuck state**, without being told
  which one it is looking at: a copy that is running, a copy that will not
  answer (ended with its whisper-server, never on its own), a whisper-server
  left holding the transcription port with no parent, one that will not die, a
  process id that has been reused by something else since, and a port held by a
  program that is not ours — which it names and refuses to touch. Also that
  every one of those answers names the exact command to type.
* **That every child process is handed to the guard that contains it** — the
  first one, the one that replaces a crash, and the one an idle release brings
  back. The containment itself is Windows' job and is proved on Windows; that
  nothing slips past it is proved here.
* **What the tray icon says and offers**: the tooltip, the status line, the
  nine menu items — eleven when a dictation history is being kept, and neither of
  the two extra ones when it is off — that each names the command that does the
  same thing, and that the icon's own bytes are an icon Windows can read whose
  colour is the status.
* **What the tray does about starting at logon**: that the item shows the state
  it was given rather than one it assumed, that off is one click exactly as on
  is, that an answer Windows would not give is said as "cannot tell" instead of
  "off", and that which way a click goes is read at the moment of the click
  rather than from the label that was drawn.
* **That nothing dictated can press Enter**, from the shape of whisper-server's
  own reply through the cleanup pass to the key events: the segment delimiter
  that produced the stray Enters, the flattening that would catch any other line
  break, and that a Return takes an explicit `[paste] line_breaks = "return"` —
  including through a spoken "new line", which is the one rule that inserts one
  on purpose.
* **What the tray does about the hotkey**: which combinations it offers and why,
  that the one in use is always among them and ticked, that a change is refused
  mid-sentence, and that the config file it writes comes back with one line
  different — comments, byte order mark and CRLF endings intact.
* **That an update chosen from the icon runs somewhere else** — a separate
  process, with a console of its own, breaking out of the job that would take it
  down when this copy stops. Doing the work inside the copy being replaced is
  the one shape that cannot work, so that is what these tests hold in place,
  along with a second update being refused while one is running and offered
  again once that one has ended.
* **Everything `dictate update` decides**, driven with real archives built and
  unpacked in the test: which folder is the install, what changed between two
  revisions in plain language, which files to write and which to remove, which
  files *he* has edited (and the refusal that names them), an archive containing
  a path that escapes its folder, a new version that will not import being rolled
  back by itself, an update interrupted half way being put back from its journal,
  and that a copy in the middle of an utterance is left alone rather than stopped.
  Also that nothing token-shaped can reach the screen.
* That the package ships no test doubles.

### Verified on real Windows — by CI, on every change

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on GitHub's hosted
Windows machines. These are things that used to be on the "never run" list:

* **whisper.cpp compiles from source with the Vulkan backend on Windows**, and
  the Vulkan backend really is in the build — not merely asked for. The CI job
  runs `setup.ps1 -Only build`, the same code this PC runs, so a difference
  between them is a difference in the PC and not a stale script.
* **The binary that comes out transcribes audio correctly** (`assets/jfk.wav`,
  through a small model, on the processor).
* **dictate starts and drives that real `whisper-server` end to end** — the
  resident process supervision, the health wait, the WAV encoding and the HTTP
  client, all against the actual compiled binary.
* **`pip install -e ".[windows]"` resolves and installs on Windows**, and
  sounddevice, numpy, sherpa-onnx and global-hotkeys all import.
* **The streaming caption model loads and produces caption text** from the
  bundled clip, and closing a caption session really does destroy that text.
* **The installer's own logic**, on Windows PowerShell 5.1 — the version that
  ships with Windows — including a download that is genuinely interrupted
  half-way and resumed, and the edit it makes to your config file.
* **That a real, installed Vulkan SDK is found without `VULKAN_SDK` being set.**
  The Vulkan build job installs the actual LunarG SDK, then clears the variable
  and checks the installer still finds the SDK on disk and points itself at it.
  That is the state that used to stop the install dead with a message telling
  you to restart the PC.
* **That a genuinely absent Vulkan SDK is reported as absent** — CI asserts the
  installer never claims it installed, never asks for a restart, and always
  prints a download address.
* **That Windows Task Scheduler accepts the logon task and removing it leaves
  nothing behind.** CI runs the real `dictate autostart enable`, reads it back
  with `dictate autostart status`, then `disable`s it and checks Windows agrees
  it is gone. What that does *not* prove is the part that needs a logon — see
  below.
* **The 984 tests above, on Windows** as well as on Linux — which is where the
  single-instance lock is exercised against Windows' own byte-range locking
  rather than Linux's `flock`.
* **That a supervised child process cannot outlive its parent.** CI starts a
  parent that starts a child through dictate's own `ManagedProcess`, then ends
  the parent with `Stop-Process -Force` — a `TerminateProcess`, so *none* of
  dictate's shutdown code gets to run — and requires the child to be gone and
  its port free within seconds. The same test with the containment removed shows
  the child surviving, which is what makes the first result mean anything. This
  is the mechanism behind the orphaned `whisper-server`; the exact keystroke
  path (Ctrl+C, then `Y` at "Terminate batch job") is on the list below.
* **That `dictate stop` really ends a copy that will not answer.** On Windows
  the suite starts a second process that takes the lock and then ignores every
  request to stop — the state that used to mean Task Manager — and `dictate
  stop` ends it for real, through the real `taskkill /T /F`, and the lock comes
  free. On Linux, where dictate has no way to end it, the same test requires it
  to say so and exit non-zero rather than claim it did something.
* **That a blank line from a program is not turned into a .NET type name.**
  PowerShell wraps every stderr line in an error record, and a *blank* one used
  to come out as the text `System.Management.Automation.RemoteException` in the
  middle of the install report. Both halves of that are tested on the real
  Windows PowerShell 5.1 that ships with Windows.
* **That an install file another program is holding open is survived, and then
  reported honestly.** CI installs dictate, has a *second real process* take a
  *real handle* on the `Scripts\dictate.exe` that came out, and runs
  `setup.ps1 -Only install` against it — twice. Once with a holder that lets go,
  where setup has to wait it out and carry on; once with a holder that never
  does, where setup has to stop, name the file, name the process Windows says is
  holding it, and never mention the internet. This is the failure that stopped
  the product owner updating, and a machine with no Windows cannot produce it:
  file locks are mandatory there and advisory everywhere else.
* **That `dictate update` really fetches this repository.** CI signs the
  GitHub CLI in with the workflow's own token, makes a throwaway editable install
  that is deliberately behind, and runs the real command: the source archive for
  a real revision comes back, a file that is behind is brought forward, a file no
  longer in the source is removed, the revision is recorded, and the commit
  subjects between the two revisions are reported. Then `--force` past an edited
  file, `--restore` back again, and — with the token cleared — that it names
  `gh auth login` instead of failing obscurely. What it cannot prove is the
  sign-in itself; see below.

### Still not verified — needs this actual PC

Nothing below has been run anywhere. It is where to look first if something
misbehaves.

* **Ctrl+C, then `Y` at "Terminate batch job (Y/N)?"** — the keystrokes that
  produced the orphaned `whisper-server` in the first place. What is proved on
  CI is the mechanism underneath that case, a parent dying with no chance to
  clean up; nobody has typed those two keys at a real `dictate run` with a real
  whisper-server holding a real 1.6 GB of VRAM.
* **Clicking away mid-dictation, on your PC.** Everything dictate *decides* in
  that case is tested here and the reported bug is a test now — but the two
  Windows calls the decision is made from (`GetForegroundWindow` at the moment
  the words are ready, and `IsWindow` on the window you started in) have never
  been run, and neither has putting the text on the clipboard when a paste is
  refused. The check is one dictation: press the hotkey in your terminal, click
  into a browser while you talk, stay there, and see that the panel says **Not
  pasted**, that nothing is typed into the browser, and that Ctrl+V in the
  terminal gives you the sentence. Then do it again clicking straight back, and
  see that it pastes as it always did.
* **Spoken punctuation in your own voice.** The transcripts it is designed
  against are real Whisper large-v3-turbo output, but the speaker was a
  synthetic voice, not you, on a processor rather than your card. That matters
  here more than it usually would: how Whisper punctuates a spoken mark depends
  on the pause you leave around it, and the same sentence read fluidly and read
  with pauses came back written two different ways — both handled, but nobody
  knows which of the two your dictation looks like. `dictate punctuate` over a
  handful of your own transcripts is the check, and it needs no microphone.
* **The icon by the clock.** Whether it appears, what it looks like at the size
  Windows draws it, whether the menu opens and closes properly, and whether
  Stop and Restart do what they say. A hosted runner has no notification area
  and no one to click anything in it. The `[tray] enabled = false` line in your
  config turns it off if it misbehaves; nothing else depends on it.
* **Choosing an update from that icon.** Everything it decides is tested, and
  what it starts is `dictate update`, which CI does run for real. What nobody
  has seen is the click: that a console window really appears when a copy
  running under `pythonw.exe` asks for one, that the icon really goes and comes
  back across the restart, and that the window is still there to read
  afterwards. From a PowerShell window the same two commands are proved.
* **`gh auth login` in a browser, and Windows remembering it.** CI borrows the
  workflow's own token, so the one interactive step of `dictate update` — the
  sign-in you do once — has never been run by anybody. Everything after it is
  proved; that first minute is not.
* **Anything GPU.** CI machines have no graphics card, so every transcription
  above ran on a processor. No timing figure here is measured. The 0.4–1.0 s
  estimate comes from a published RX 6750 GRE (RDNA2, Windows, Vulkan) encode of
  428 ms on a card with half the compute units. **Step 6 of setup answers this
  in about a minute**, and so does `dictate transcribe some.wav` at any time.
  A green tick on CI says nothing whatsoever about the GPU.
* **That the 1.6 GB really comes back.** dictate shuts `whisper-server` down and
  the process genuinely exits — that much is tested against a real child process
  here. Whether the driver then returns the VRAM to the pool, and how long the
  reload actually takes on your card, has been observed by nobody. Task
  Manager's Dedicated GPU memory figure, five minutes after you stop dictating,
  is the check.
* **Which program was actually holding `Scripts\dictate.exe` on your PC.** The
  install now waits for it and, if it will not let go, asks Windows itself
  through the Restart Manager and prints the answer — but that answer has only
  ever been read on a CI runner, where the holder was a test. On your machine it
  could be a `dictate.exe` launcher that outlived the copy it started, or your
  antivirus scanning a freshly written executable, and those want different
  remedies. **The next time it happens, the message names the program: that line
  is the measurement, and nobody here can take it for you.** If it does not
  happen again, the wait absorbed it and there is nothing to chase.
* **The toolchain install** — winget fetching Python, CMake, the Vulkan SDK and
  the C++ build tools. CI machines already have most of those and do not use
  winget at all, so step 2 of setup is the one part of it no machine here has
  ever executed. Every tool it installs is now checked by looking for the tool
  itself afterwards, never by trusting the installer's exit code and never by
  reading an environment variable as a proxy — but the winget calls themselves,
  and the direct download from LunarG that steps in when winget cannot install
  the Vulkan SDK (including the Authenticode check on what arrives), have run
  nowhere.
* **The caption overlay, as a window.** The non-activating, click-through,
  always-on-top window is built from the documented Win32 extended styles, but
  no one has watched it fail to steal focus. This is the highest-risk unverified
  piece, because if it does steal focus the paste lands in the wrong window.
* **The caption held on screen while it thinks, on a real screen.** That the
  words stay, that they go grey and read as "not final yet", that a second of it
  looks like waiting rather than like something stuck, and that the moment they
  disappear reads as the text landing. What is tested here is the decision — the
  panel is told to keep the words, told what colour to draw them, and told to
  clear them exactly when the paste happens — never how any of it looks.
  `dictate overlay` shows the whole cycle in about ten seconds.
* **The two history items on the tray menu**, like the rest of that menu: whether
  they appear, and whether clicking Delete really removes the file on his
  machine. The store underneath is tested here; `os.startfile` opening the file
  for him has run nowhere, and `dictate history` from a prompt is the fallback.
* **Changing the hotkey from the icon, as a click.** Whether the submenu opens
  and the tick shows against the right line, and — the part only his machine can
  answer — whether Windows hands over the new combination while dictate is
  running, and hands the old one back if it will not. The order is what makes
  that safe (register first, write the config only once it worked), and the
  order is tested here; the two Windows calls it is wrapped around are not.
  `dictate hotkey "…"` from a prompt is the fallback, and CI runs that.
* **Changing the caption size from the icon**, which is the same submenu shape
  and the same one-line config write. What it decides is tested here; the click
  itself is not.
* **Whether a paste ever arrives with Ctrl still held.** dictate now waits for
  it and then forces the key up, but nobody here has held a chord through a
  paste. What it costs if the guard is wrong is a wait of at most 400 ms;
  `[paste] modifier_wait_ms = 0` turns it off.
* **What the overlay looks like, at any size.** Nobody here has run it. The
  colours, the type and the layout arithmetic were checked by rendering the
  panel at exactly the sizes the code uses — the earlier work drew it with the
  real Fira Code file against a white document, a dark editor and a photo
  wallpaper, and the size ladder was drawn from the product's own layout code
  with a stand-in monospace font — but that is a picture of the design by a
  different renderer, not a screenshot of the Tk window. Whether `compact` is
  the right *default* on his display is his call, and
  `dictate overlay --size medium` is how he makes it: it takes about ten
  seconds and asks whether to keep what he saw.
* **How Fira Code's ligatures render here.** Fira Code puts all of them in the
  OpenType `calt` feature — it has no `liga` table at all (checked against the
  6.2 release). Tk draws text through GDI on Windows, which is not expected to
  apply `calt`, so the ligatures most likely do not appear at all, which for
  prose captions is the outcome we want. That expectation has not been tested on
  a real screen. If something does look odd in a sentence, the fix is one line:
  set `font_family` to `"Fira Mono"` or anything else.
* **Per-monitor DPI and multi-monitor placement.** The rule and the arithmetic
  are tested here; the Win32 calls behind them (`SetProcessDpiAwarenessContext`,
  `MonitorFromWindow`, `GetDpiForMonitor`) have run nowhere. A mixed-DPI desktop
  is the case to watch. `dpi_awareness = "off"` and `follow_focus = false` are
  both one-line retreats if either misbehaves.
* **The global hotkey**, including whether press/release feels right in practice.
* **The mouse trigger's hook — every Windows call in it.** Nobody here has a
  mouse to press or a machine to install a `WH_MOUSE_LL` hook on, so
  `platform/windows/mouse.py` has never run: not the hook going in, not the
  swallow actually stopping Back, not the replayed click arriving in the window
  you clicked in, and not what any particular security product makes of a
  process installing a global mouse hook. What *is* tested here is everything
  that decides — which values name a button, the press/release state machine,
  the swallow rule and its edges (the button held when dictate starts and when
  it stops, a click too short to be a dictation, a second press with no release
  between), and that a hook which will not install leaves the keyboard chord
  registered and working. The retreat is one line: put a keyboard combination
  back in `[hotkey] combination`.
* **Microphone capture** through PortAudio — and with it, whether the new
  caption model is better *on his voice*. Everything measured about it was
  measured on read speech on a Linux box; his microphone, his room and his
  spontaneous phrasing are the case nobody here can run.
  `dictate captions some.wav` is the measurement, and it takes one recording.
* **Whether Windows is discarding input audio while he dictates.** It reports
  every occurrence per utterance now instead of counting silently, so the next
  time it happens he will be told at the release, with how many milliseconds
  went — but nobody here has produced an overflow to watch it fire.
* **Text injection** into real applications — terminals in particular vary.
* **The logon itself.** CI machines never log on, so nobody has watched the task
  fire, watched dictate come up in an interactive session, or confirmed that no
  console window appears on the way. The three things to check the first morning
  after `dictate autostart enable`: that the hotkey works without you having
  started anything, that nothing flashed on screen, and that
  `dictate autostart status` says the copy answering the hotkey **is** the one
  the logon task started. It used to say to check for `last result: 267009 (it
  is running right now)`, and that was wrong advice: 267009 is Task Scheduler
  saying it has not seen *the run* finish, which is not the same as dictate
  working — a logon start that gave up and is waiting on its message box reads
  exactly the same way. The report compares the two readings itself now, and
  `dictate autostart status --why` lists what is actually running.
* **The message box** that a failed logon start puts on screen. It is one
  `MessageBoxW` call and it is wrapped so that failing to show it cannot change
  anything, but it has never been displayed. The log entry behind it is written
  either way, and that part *is* tested.
* **The three places that now offer to start dictate at logon**, as seen. What
  each one decides is tested here; what nobody has watched is setup's question
  appearing in a real console and taking a real keypress (the timeout and the
  no-console path are tested, the keystroke is not), the **Start when I log in**
  item drawn with its tick in a real notification-area menu, and a click on it
  reaching Task Scheduler and coming back with the tick the other way round.

If any of it is wrong, `dictate doctor` and the log file are the two things to
look at, and the failures should be legible rather than silent.

---

## Credits

[`PinW/whisper-key-local`](https://github.com/PinW/whisper-key-local) (MIT) was
read as prior art for the Windows mechanics — the `platform/<os>/` package split,
the layout-independent `SendInput` + `KEYEVENTF_UNICODE` approach to typing text,
and `global_hotkeys` as the way to get separate key-press and key-release
callbacks. No code was copied. It has no caption overlay (it uses a system tray
and a terminal UI), so that window is built from the Win32 primitives directly.

[whisper.cpp](https://github.com/ggml-org/whisper.cpp) (MIT) and
[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) (Apache-2.0) do the actual
transcription.

## Further reading

`docs/DESIGN.md` — the settled decisions, the six constraints that break the
product if ignored, the threading model, and what was deliberately not done.
