/** Block destructive whole-file writes that small models commonly perform from memory. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent"
import { existsSync, statSync } from "node:fs"
import { isAbsolute, relative, resolve } from "node:path"
import { SHELL_TOOLS, detectWriteTargets } from "./_shared/write-guard-shell-write.ts"

const ALWAYS_GUARD_BYTES = 40000
const read = new Set<string>()

function insideTree(path: string, root: string): boolean {
  const rel = relative(root, path)
  return rel !== "" && !rel.startsWith("..") && !isAbsolute(rel)
}

function pathFrom(input: Record<string, unknown>): string | undefined {
  if (typeof input.path === "string") return input.path
  if (typeof input.file_path === "string") return input.file_path
  return undefined
}

function verdict(raw: string, root: string, truncates = true): string | null {
  if (!truncates) return null
  const path = resolve(root, raw)
  if (!insideTree(path, root) || !existsSync(path)) return null
  try {
    const info = statSync(path)
    if (!info.isFile()) return null
    if (read.has(path) && info.size <= ALWAYS_GUARD_BYTES) return null
    return read.has(path)
      ? `it is ${info.size} bytes, too large to rewrite accurately from memory`
      : "you have not read it this session"
  } catch {
    return null
  }
}

function refusal(tool: string, raw: string, why: string): {block: true; reason: string} {
  return {
    block: true,
    reason:
      `${tool} refused: ${raw} already exists and ${why}. ` +
      `Use edit so lines you did not name remain intact. If whole-file ` +
      `replacement is intentional, read the file first, then write it.`,
  }
}

export default function writeGuard(pi: ExtensionAPI): void {
  pi.on("tool_call", (event, ctx) => {
    const name = String((event as any).toolName ?? "")
    const input = ((event as any).input ?? {}) as Record<string, unknown>
    const root = ctx.cwd

    if (name.toLowerCase() === "read") {
      const raw = pathFrom(input)
      if (raw) read.add(resolve(root, raw))
      return
    }
    if (name.toLowerCase() === "edit") {
      const raw = pathFrom(input)
      if (raw) read.add(resolve(root, raw))
      return
    }
    if (name.toLowerCase() === "write") {
      const raw = pathFrom(input)
      if (!raw) return
      const why = verdict(raw, root)
      return why ? refusal("write", raw, why) : undefined
    }
    if (!SHELL_TOOLS.has(name)) return

    const command = input.command
    if (typeof command !== "string" || !command) return
    for (const target of detectWriteTargets(command)) {
      const why = verdict(target.path, root, target.kind !== "append")
      if (why) return refusal(`shell ${target.kind}`, target.path, why)
    }
  })
}
