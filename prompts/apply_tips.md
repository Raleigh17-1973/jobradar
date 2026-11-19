# Form tips by applicant system

What filling real applications with the Chrome DevTools tools (`mcp__chrome-devtools__*`) has taught so far. Each
tip cost a failed field or a lost form once. Add to it when a site surprises you.

## Chrome DevTools tools
- Every tool takes a `pageId`; `new_page` returns the page list with ids. Work only in pages you opened.
- `take_snapshot` with `filePath` only writes inside the server's `--workspace` folder (the applications folder).
  Write a hidden `.snap.txt` into the job's folder, grep it, then delete it. Re-snapshot after a file upload: uids shift.
- `upload_file` on the button that opens the file chooser works directly, including LinkedIn Easy Apply.
- `Meta+A` doesn't select text. To replace a value: `el.focus(); el.select()` in `evaluate_script`, then `type_text`.
- `fill` / `fill_form` sometimes shows the value but the page's framework ignores it, so it fails validation as
  empty (Greenhouse's new UI, Ashby textareas, salary inputs). Click the field, `select()` it, then `type_text`.
- A hidden checkbox can't be clicked; click its label text.
- Avoid `wait_for` on long pages such as Easy Apply's résumé step: it returns the whole snapshot.

## Greenhouse
- A job page that redirects to the company's own site can be filled at
  `https://job-boards.greenhouse.io/embed/job_app?for=<board>&token=<job id>`.
- Questions before opening: `boards-api.greenhouse.io/v1/boards/<board>/jobs/<id>?questions=true`.
- Dropdowns are react-select: a real click on the combobox, then a JS `.click()` on the visible `[role=option]`.
  The phone-country list is always in the DOM, so pick only visible options (offsetParent set, height > 0).
- "Please identify your race" appears only after Hispanic/Latino = No.
- Invisible reCAPTCHA usually passes on its own. An image challenge is the user's.

## Ashby
- Fields before opening: POST `jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting` with
  `organizationHostedJobsPageName` and `jobPostingId`. It misses some live-page fields (SMS radios, consent boxes).
- Press Yes/No buttons and Submit with JS `.click()`: a real click can land on the sticky company logo and leave.
- Long textareas: focus, select, `type_text` (see above).

## Lever
- The location box needs a typed value, then mousedown/click on `.dropdown-location`.
- Submit often shows an hCaptcha puzzle: that's the user's.

## LinkedIn Easy Apply
- The modal isn't reachable by `querySelector`; use snapshot uids.
- Work entries need a City. Education years can't be cleared once edited.
- The privacy checkbox needs a click on its "I consent" label. Leave "Follow <company>" unticked.

## Others with no account
- **Pinpoint:** react-select; a JS ArrowDown keydown on the input, then mousedown/mouseup/click on the option.
  Radios: use the radio's uid, not its text's.
- **Gem:** "We couldn't verify your submission" after a CAPTCHA: reload, refill, "Apply without saving".
- **Dayforce:** choose "Apply without an Account". antd selects open with a real click.
- **SilkRoad:** the form is at `/careers/Apply/MultiForm/<id>`.
- **Workday as a guest:** some tenants let you apply without signing in and offer an optional password box at the
  end. Leave that box empty.
