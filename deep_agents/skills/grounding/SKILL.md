---
name: grounding
description: How to check a claim against the printed page, when to cite a page number, how to find the chapter's own printed questions, and what to do when the textbook does not say the thing you were about to assert. Load before asserting anything about what the chapter contains.
---

# Grounding

Everything this system writes about a chapter must be traceable to the chapter.
The tools give you the actual pages; there is no reason to work from memory of
what a textbook like this usually says.

## The tools, in the order you need them

1. `get_book_order` - which pages exist, and how the chapter was cut into topics.
   Call this first; everything else takes a page number from it.
2. `get_page_range(start, end)` - the verbatim text, page markers intact.
3. `search_book(phrase)` - does the book ever actually say this?
4. `list_figures` / `get_figure` - what is printed beside the text.

## Page markers are load-bearing

The text you get back keeps its `<!-- page N -->` markers. They are not noise:

- The Concept section of a prep material cites them as `(Page N)`.
- The validator treats the sliced text as **the only thing a Concept bullet is
  allowed to be grounded in**.

So a citation written from a marker you can see is checkable. One inferred from
"this is probably around page 14" is not, and it will fail validation later at
much greater cost than checking now.

## The question to ask before every assertion

**Does the book say this, or am I supplying it?**

Both are legitimate - this stage exists precisely to supply what the book does
not say - but they are different and must not be mixed up:

- *The book says it.* Cite the page. Do not paraphrase away the wording the
  child will actually see.
- *I am supplying it.* Say so plainly and ground it in something the book does
  show. "The chapter never states that the remainder is not an error, but every
  worked example on pages 14-16 divides evenly" is a real, checkable inference.
- *Neither.* Do not write it.

## The printed questions are part of the chapter

Exercises, "do it yourself" boxes, fill-in-the-blanks, end-of-chapter questions
and the captions under figures are what the child is examined on, and they are
frequently the least-read pages in the pipeline. Locate them deliberately:
`get_book_order` for where they sit, `get_page_range` for their verbatim
wording, `search_book` for a question form you think exists.

Two things follow:

- **Quote the wording, do not paraphrase it.** A question the child will see as
  "Match the following" is not "children match items". The exam's phrasing is
  itself content (`assessment-alignment`).
- **Never cite a question number you have not read.** Exercise numbering shifts
  between print runs more often than body text does.

## When the page is thin

Textbook pages are frequently thinner than the topic they are supposed to teach:
a heading, two worked examples, six exercises. That is normal and it is not a
reason to invent content.

Say what is actually there. A downstream stage can act on "the page gives two
worked examples and no explanation of why the rule holds". It can do nothing
useful with a confident summary of an explanation that is not printed, except
reproduce it in front of a class that will then be examined on the real book.

## Secondary sources are leads, not citations

A teacher's own review, a lesson-plan critique, a colleague's notes, or an
earlier draft of this material will often reference specific pages - "the
top-view drawing on Page 2", "the overlapping-triangle figure on Page 14", "we
unfolded the box like Page 10 showed". These are worth reading and worth
following up. They are not a citation you may repeat.

They were written from someone's reading or memory of the book, not from
`get_page_range` output, and they drift the way any secondhand reference does:
a page number misremembered, a figure attributed to the wrong section, a print
run with different pagination than the one the tools index. Treat every page
number that did not come from this chapter's own tool output as a claim to
check, exactly like any other assertion under "does the book say this, or am I
supplying it."

Before a page number from such a source reaches a Concept bullet or any other
citation, run it through `get_page_range` or `search_book` yourself. If it
confirms, cite the page you checked - not the document that mentioned it. If it
does not, you have caught a secondary-source error before it became an
ungroundable claim, and you write what the page actually shows instead.

## Related

- `curriculum-reasoning` - what to derive once you know what the page says
- `assessment-alignment` - what the printed questions are for
- `core-pedagogy` - why the textbook is the syllabus here
