# Containerfile for latexdiff-zip.
#
# Bundles every dependency (TeX Live, latexdiff/latexpand, ImageMagick,
# Ghostscript, poppler, python3) so the engine runs identically on any host.
#
# Build:  podman build -t latexdiff-zip .
# Run:    podman run --rm -v "$PWD":/work:Z latexdiff-zip old.zip new.zip
#
# The engine is latexdiff-zip.py, a stdlib-only Python program: it unpacks zip
# and tar archives and fetches arXiv sources over HTTPS itself, so no unzip,
# tar, bzip2/xz or curl is needed. The official TeX Live image already ships
# latexdiff, latexpand, pdflatex, bibtex and biber; we add python3 (the engine),
# the figure-comparison tooling, and ca-certificates for HTTPS verification.
FROM texlive/texlive:latest

RUN apt-get update && apt-get install -y --no-install-recommends \
        imagemagick \
        ghostscript \
        poppler-utils \
        python3 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Debian's default ImageMagick policy blocks reading/writing PDF, PS and EPS for
# security. We rasterise figure PDFs/EPS to PNG, so re-enable those coders.
RUN for f in /etc/ImageMagick-6/policy.xml /etc/ImageMagick-7/policy.xml; do \
        if [ -f "$f" ]; then \
            sed -i -E '/rights="none" pattern="(PDF|PS|EPS|PS2|PS3)"/d' "$f"; \
        fi; \
    done

COPY latexdiff-zip.py /usr/local/bin/latexdiff-zip
RUN chmod +x /usr/local/bin/latexdiff-zip

# Default to a world-writable HOME so TeX can write its caches under any UID
# (the wrapper runs the container as the calling user via --userns=keep-id).
ENV HOME=/tmp
WORKDIR /work
ENTRYPOINT ["latexdiff-zip"]
