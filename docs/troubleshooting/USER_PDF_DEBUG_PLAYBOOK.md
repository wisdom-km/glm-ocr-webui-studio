# PDF Selfhosted Debug Playbook

Date: 2026-03-19

This document is written for you as the operator of this repository. It records what you did, what we solved together, and what you should do the next time a long PDF behaves badly.

## 1. What Happened In This Case

The reproduced file was:

`C:\Users\19612\Documents\Playground\glmocr_project\glm_ocr_inputs\荣格分析心理学导论.pdf`

Observed pattern:

- single page 12 succeeded
- range `12-20` originally failed around page 15
- full range `12-133` originally appeared to stall or timed out

What this proved:

- page range selection was working
- the whole backend was not dead
- the problem was tied to complex pages in the selfhosted pipeline

## 2. What You Did That Helped

You materially improved the debugging process by doing these things:

### 2.1 You kept the reproduced file fixed

You did not keep swapping PDFs too early. That let us compare:

- single-page behavior
- short-range behavior
- long-range behavior

### 2.2 You kept sending real logs and screenshots

That let us line up:

- GUI state
- runtime logs
- local server logs
- output folders

### 2.3 You kept challenging misleading explanations

You repeatedly separated:

- fake progress
- save/output problems
- backend timeout problems
- page-range problems

That was the right instinct.

## 3. What We Solved Together

### 3.1 We confirmed the page range was real

The code already rendered only the selected range, not the whole book.

### 3.2 We exposed the real backend state

We added enough logging to identify:

- current page
- current region
- request ID
- backend wait state
- timeout state

### 3.3 We fixed the local service startup problem

There was a separate local model-loading issue that had to be solved before the PDF-specific failure could be isolated cleanly.

### 3.4 We changed the failure boundary

This was the main reliability improvement.

Instead of one giant selfhosted parse session owning the full rendered page range, the high-risk path now processes rendered PDF pages page-by-page.

That means:

- one bad page is no longer allowed to kill the whole job silently
- retries and restarts can happen at page granularity
- partial outputs survive better

## 4. The Main Lesson For Next Time

Do not trust the first visible symptom.

With long OCR jobs, the first visible symptom may be:

- bad progress text
- wrong ETA
- output folder looks incomplete
- GUI says waiting

But the true cause may be lower down:

- local service startup bug
- heavy page with too many regions
- one `generate()` request taking too long
- one page poisoning a long parse session

## 5. What To Do Next Time A PDF Fails

Follow this order.

### Step 1. Confirm the input boundary

Check whether the selected page range really became the expected rendered page count.

### Step 2. Confirm the backend is alive

Look for:

- backend ready
- local server status checks
- request start / generate start lines

### Step 3. Find the first failing page

Do not start with the whole book unless the short range already behaves.

Use:

- one page
- then a short range
- then the full range

### Step 4. Find the stuck request

Look for:

- `request_id`
- `page`
- `region`
- `elapsed_wait`

This tells you whether the job is actually frozen or just slow.

### Step 5. Only then adjust output or UI expectations

Do not treat progress text as the root cause until the backend state is known.

## 6. How To Read The Logs

### 6.1 Runtime GUI log

Folder:

`logs/runtime/`

Most useful file:

- `glm_ocr_web_gui.log`

Use it to answer:

- which page is active
- whether retries happened
- whether the service restarted
- whether the task failed or finished

### 6.2 Local backend log

Folder:

`logs/runtime/`

Most useful file:

- `glm_ocr_local_server.log`

Use it to answer:

- whether the local service started correctly
- which request entered `generate()`
- whether `generate end` ever appeared

### 6.3 One-off debug logs

Folder:

`logs/debug/`

Use this folder for:

- smoke tests
- temporary validation runs
- manual incident debugging

## 7. What You Should Do If It Fails Again

### If a single page fails

- keep the page number
- keep the request ID
- keep the region number if present
- rerun that page alone first

### If a short range fails

- narrow to the first failing page
- compare that page with nearby pages that succeed

### If the local service looks bad

- restart the service
- rerun one known-good page
- confirm startup and generate logs are back

### If the job looks frozen

Do not rely on the progress bar alone.

Check whether logs are still advancing at:

- GUI runtime level
- local server request level

## 8. What Not To Do

- Do not immediately assume the page-range feature is broken.
- Do not immediately increase timeouts without identifying the failing page.
- Do not switch datasets too early.
- Do not debug only from the GUI if local server logs are available.

## 9. The Reusable Debug Pattern

When a long PDF fails, reuse this pattern:

1. confirm selected pages
2. confirm backend readiness
3. isolate first failing page
4. inspect request ID / region
5. decide whether the failure is:
   - startup
   - page complexity
   - request timeout
   - save/output
6. only then decide whether to change:
   - code
   - timeout policy
   - retry policy
   - OCR settings

## 10. Short Version

The fastest correct mental model is:

- first prove the page range is right
- then prove the backend is alive
- then find the first failing page
- then find the stuck request
- only after that worry about UI polish
