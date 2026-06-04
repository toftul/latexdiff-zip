# Diffing directly in Overleaf (no download)

[← back to README](README.md)

If you'd rather stay inside Overleaf, you can run `latexdiff` there without exporting any
zips. See [Overleaf's guide](https://www.overleaf.com/learn/latex/Articles/How_to_use_latexdiff_on_Overleaf).

The method I like: keep the old version of your main file in the project (e.g.
`monoclinic_CD_first_submit.tex`) next to the current one (`monoclinic_CD.tex`), add a
`diff.tex` file with the content below, and compile **`diff.tex`** as the main document. It
shells out to `latexdiff` at build time and `\input`s the result:

```latex
% based on
% https://tex.stackexchange.com/a/603351/249682

\RequirePackage{shellesc}

\newcommand{\oldFile}{monoclinic_CD_first_submit}
\newcommand{\newFile}{monoclinic_CD}

\ShellEscape{latexdiff "\oldFile.tex" "\newFile.tex" > diff_result.tex}

\input{diff_result}
\documentclass{dummy}
```

Set `\oldFile` / `\newFile` to your two filenames. It relies on shell-escape (on by default
in Overleaf), diffs a single `.tex` rather than a flattened project, and makes no figure
collages — but needs no local tools and no downloads.
