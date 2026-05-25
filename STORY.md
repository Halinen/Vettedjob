# Vetted Job

## Inspiration

I was job hunting and kept hitting the same wall: too many "remote" postings were obviously sketchy. Upfront "training fees." \$800/day for data entry. Listings that had clearly been up for a year. One said "remote" in the title and "must relocate" in paragraph six.

I'd written a script to score jobs for *fit* — does this match my résumé? But that's the wrong question. A perfect-fit job from a company that doesn't exist is worse than useless. The question I actually wanted answered was: **is this job even real?**

## What it does

You give it a job posting — paste one in, or let it pull from Indeed/Google Jobs — and it runs four checks, then tells you `pass`, `review`, or `reject` with a 0–10 score. Every red and green flag comes with the receipt: a quoted line or a URL. No "trust me, it's a scam."

The four layers, cheapest first:

1. **Rules** — regex for upfront fees, weird salaries, free-mail recruiters, scam phrasing. Free, instant.
2. **Web search** — Claude actually goes and checks: does this company have a real site? A Glassdoor history? Is the role on their own careers page?
3. **LLM judge** — Claude scores scam risk, ghost-job risk, and whether "remote" is honest, with evidence.
4. **Aggregate** — rules can hard-veto; the model fills in the nuance.

## How I built it

The whole thing is layered so cost and trust line up. Rules run first because they're free and certain — if a posting demands a "\$200 registration fee," it's capped at 3/10 no matter how slick the writing is. Claude only gets called when the rules *don't* already settle it, so obvious scams cost \$0.

The rule I'm proudest of is small: **every flag the model emits has to cite a source, or it gets thrown away.** That one constraint is the difference between a chatbot opinion and something I'd actually act on.

I kept the core (`assess(job)`) standalone from the start, so wrapping it in a FastAPI web app later was almost free — no rewrites.

## Challenges

- **Not crying wolf.** The hard part wasn't catching scams, it was *not* flagging every small company that just has no Glassdoor page. That's what the `review` middle ground is for — "can't verify" isn't "fraud."
- **`UnicodeEncodeError`.** First real run crashed on a `™` character because Windows doesn't default to UTF-8. Classic bug you only find by actually running it.
- **`"worldwide"` is not a country.** The scraper returned zero jobs for ages — Indeed routes by country and I was passing a string it didn't recognize.
- **Gmail told me "no" three times.** Not a code bug — App Password / 2FA setup. A good reminder that "anyone can deploy this" lives or dies on the boring parts.

## What I learned

The model is one layer out of four, not the whole thing. Cheap rules below it, transparent math above it. And the most useful thing Claude does here isn't scoring — it's *citing its sources*. Designing it so the model literally can't hand back an unsourced accusation is what made this feel trustworthy instead of just clever.
