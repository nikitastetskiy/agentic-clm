#!/usr/bin/env bash
# git credential helper for the pipeline: answers git's password prompt with the token from the
# environment (see ci_git_push in ci/lib.sh). Prints nothing else.
printf '%s' "${GIT_TOKEN:-}"
