# choobi baseline style guide

<!--
choobi's built-in writing style: the "just good" defaults that ship with every install.
A repo's own SOP and a personal ~/.choobi/style.md override this file; anything they leave
unspecified falls back here. Synthesized from the Google developer documentation style
guide, the Diátaxis framework, and Stripe's documentation practices.
-->

## Contents

- [Voice and tone](#voice-and-tone)
- [Clarity and brevity](#clarity-and-brevity)
- [Structure: put things where they belong](#structure-put-things-where-they-belong)
- [Formatting](#formatting)
- [Punctuation and mechanics](#punctuation-and-mechanics)
- [Accuracy: never ship something untrue](#accuracy-never-ship-something-untrue)
- [The pre-write checklist](#the-pre-write-checklist)

## Voice and tone

Write like a knowledgeable colleague who respects the reader's time. Be direct and factual.

- Use the second person ("you"), not "we".
- Use the active voice, and name who does what.
- Use the present tense: "the function returns", not "the function will return".
- Cut filler: avoid "please", "simply", "just", "note that", and "in order to".

## Clarity and brevity

Say the most in the fewest words. Every sentence should earn its place.

- One idea per sentence. Prefer short sentences.
- Lead with the point, then explain. State the outcome first, as in "To accept a payment, do X".
- Define a term inline the first time you use it, in one sentence, so the reader never has
  to leave the page to understand it.
- If the document is still correct and complete without a sentence, delete it.

## Structure: put things where they belong

Different reader needs want different documents. Match the change to the right kind of doc:

- **Tutorial**: a guided first success for a newcomer.
- **How-to**: steps that solve one real task for a reader who already knows the basics.
- **Reference**: complete, accurate facts (flags, fields, signatures) with no narrative.
- **Explanation**: the why, the background, and the design decisions.

Update the document that already owns a topic before creating a new one. Keep reference and
explanation apart: do not bury a fact the reader needs inside a wall of prose.

## Formatting

- Use sentence case for titles and headings ("Getting started", not "Getting Started").
- Use exactly one H1, and do not skip heading levels.
- Put a table of contents at the top of any long document, and update it whenever you add,
  remove, or rename a heading.
- Use numbered lists for sequences and bulleted lists for everything else.
- Put conditions before instructions: write "If X, do Y", not "Do Y if X".
- Put code, paths, commands, flags, and identifiers in `code font`.
- Give every code block a language, and keep examples real and runnable.
- Use descriptive link text (name the destination), never "click here" or "this link".
- Use tables for pairs of related facts, and keep them narrow enough to scan.

## Punctuation and mechanics

- Do not use em dashes. Rewrite with a comma, a colon, parentheses, or two sentences.
- Use the serial (Oxford) comma.
- Use standard American spelling and punctuation.
- Expand an acronym on first use unless it is universally known.

## Accuracy: never ship something untrue

Documentation that lies is worse than no documentation. Verify every claim before you write it.

- State only what the code, the diff, or the conversation actually supports. If you cannot
  verify a claim, do not make it.
- Check that every path, link, command, flag, and symbol you mention exists and resolves.
- Make the smallest change that captures what changed. Do not rewrite a whole document to
  correct one fact.
- When you change one place, update everything that depends on it: the table of contents,
  cross-references, examples, and any summary at the top.
- Re-read the final text once and ask: is anything here untrue, out of date, or missing?

## The pre-write checklist

Before committing a documentation change, confirm:

1. It lives in the right document and the right section.
2. Every fact is verified against the source.
3. The formatting follows the rules above, and the table of contents is current.
4. It reads clearly and minimally, with no em dashes and no filler.
5. Nothing that changed was left undocumented, and nothing untrue was added.
