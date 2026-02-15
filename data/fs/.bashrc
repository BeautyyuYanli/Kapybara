export TERM=dumb
export NO_COLOR=1
export CLICOLOR=0
export FORCE_COLOR=0         # many Node tools
export NODE_DISABLE_COLORS=1
export PAGER=cat
export LESS='-FRSX'          # if something still invokes less
export GIT_PAGER=cat
export LC_ALL=C.UTF-8        # or C, depending on your needs
export LANG=C.UTF-8
export TZ=UTC 
export PATH="$HOME/.local/bin:$PATH"
stty -echo; 
set -a; 
. ~/.env; 
set +a; 

# Token-efficient JSON for LLM prompts:
# - `-c`: compact (no pretty whitespace)
# - `-M`: monochrome (no ANSI color codes)
# - default filter is `.` so pipelines can use `| jq` instead of `| jq .`
jq() {
  if [ "$#" -eq 0 ]; then
    command jq -cM .
  else
    command jq -cM "$@"
  fi
}
