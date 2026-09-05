# Bumping the version
There is no single workspace version. The root `pyproject.toml` is a virtual workspace root with no `[project]` table, so `uv version` has nothing to act on and there is no `--all-packages`. 

Bump each member:

```sh
for p in illusion-core claws lipgloss illusion-bot illusion-kiosk; do
    uv version --package "$p" --bump patch
done
```

Use `--bump minor`/`--bump major`, or an explicit `uv version --package "$p" 1.6.0`, as needed.