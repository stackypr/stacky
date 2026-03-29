def _stacky_build_info_impl(ctx):
    out = ctx.outputs.out
    ctx.actions.run_shell(
        inputs = [ctx.info_file],
        outputs = [out],
        arguments = [ctx.info_file.path, out.path],
        command = """
set -euo pipefail
commit="$(awk '/^STABLE_GIT_COMMIT / {print $2; exit}' "$1")"
if [[ -z "${commit}" ]]; then
  commit="unknown"
fi
echo "STACKY_BUILD_COMMIT = '${commit}'" > "$2"
""",
    )
    return [DefaultInfo(files = depset([out]))]


stacky_build_info = rule(
    implementation = _stacky_build_info_impl,
    attrs = {
        "out": attr.output(mandatory = True),
    },
)
