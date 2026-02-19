To develop Trust5, you need to respect the following:

1-  no python code more than 500 lines
2-  should pass 'basedpyright' without ignoring warnings or errors -- unless absolutely impossible or related to thirdparty libs
3-  no new library dependencies
4-  the workflow logic should be done in 'stabilize'. Stabilize is a glassbox, which will operate tasks (or blackboxes)
5-  'mypy --strict' and 'ruff check' should pass without errors -- no ignore unless related to thirdparty libs and not possible to fix
6-  Code coverage is not very important, but add tests if you find a bug to avoid regression
7-  put good defaults. Trust5 should be able to run by non-nerd people as well.
8-  develop Trust5 with the methods Trust5 itself promotes: spec -> test -> implement -> repair -> quality -> done
9-  if you add providers, try not to hardcode models, auto model discovery would be the best
10- ask me (Farshid) if you are brilient idea and don't know where to start. I'll be more than happy to accomodate your contribution
