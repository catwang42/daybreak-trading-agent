You wrote the paragraphs below in this run's research report on {symbol} —
{name}. They quote prices or percentages that the published trade plan does not
support, and the plan is the version a reader acts on. Restate them.

## The computed plan
This arithmetic is computed by the pipeline from the run's price snapshot. It is
not open to revision here: it is what section 4 of the report prints.

{plan_table}

## What disagrees
{disagreements}

## The paragraphs
{paragraphs}

## Your task
Return each paragraph rewritten so that every price and percentage in it matches
the computed plan above.

- **Change the figures, not the argument.** Same reasoning, same conclusion,
  same tone, same length. If you argued the stop was too tight, keep arguing it —
  against the stop the plan actually uses.
- **Do not add figures the plan does not contain**, and do not recompute
  anything. If a paragraph reasoned from a number that is now different, follow
  it through: a stop that moves changes what "a close below it" means.
- **Do not mention the correction.** No "revised", no "as computed", no
  parenthetical about the earlier figure. The report prints its own audit line
  saying you restated this; a paragraph that narrates its own editing is
  unreadable.
- **Copy each `label` exactly** from the list above, and return one entry per
  paragraph listed, in the same order.

This is research for a human who makes every decision. Nothing here is
transmitted to a broker and no order will be placed from it.
