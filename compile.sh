#!/bin/bash
# Usage: ./compile.sh jobs/2026-04-05_acme_engineer/cv
# Compile cv.tex or cl.tex inside the given directory.
DIR=$1
if [ -z "$DIR" ]; then
    echo "Usage: ./compile.sh <directory>"
    echo "Example: ./compile.sh jobs/2026-04-05_acme_engineer/cv"
    exit 1
fi
cd "$DIR" || exit 1
if [ -f "cv.tex" ]; then
    pdflatex -interaction=nonstopmode cv.tex 2>&1 | tail -5
    echo "cv.pdf compiled"
fi
if [ -f "cl.tex" ]; then
    pdflatex -interaction=nonstopmode cl.tex 2>&1 | tail -5
    echo "cl.pdf compiled"
fi
