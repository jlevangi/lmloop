/** Pure shell-write detection for write-guard. No pi runtime dependency. */

export type WriteKind = "redirect" | "append" | "tee" | "dd"
export interface ShellWrite { path: string; kind: WriteKind }

export const SHELL_TOOLS: ReadonlySet<string> = new Set([
  "bash", "Bash", "ShellSession", "ShellStart",
])

const DEVICES = new Set([
  "/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/tty",
  "/dev/zero", "/dev/full", "/dev/random", "/dev/urandom",
])
const BREAKS = new Set([">", "<", ";", "&", "|", "(", ")"])
const HEREDOC = /<<-?[ \t]*(?:'([^']*)'|"([^"]*)"|([A-Za-z_][A-Za-z0-9_]*))/

function scan(cmd: string, visit: (ch: string, i: number, quoted: boolean) => void): void {
  let quote: '"' | "'" | null = null
  for (let i = 0; i < cmd.length; i++) {
    const ch = cmd[i]
    if (quote === null && ch === "\\") { i++; continue }
    if (quote === "'" && ch === "\\") { visit(ch, i, true); continue }
    if (quote === null && (ch === '"' || ch === "'")) { quote = ch; continue }
    if (quote !== null && ch === quote) { quote = null; continue }
    visit(ch, i, quote !== null)
  }
}

export function stripHeredocBodies(cmd: string): string {
  let out = cmd
  let from = 0
  for (let guard = 0; guard < 32; guard++) {
    const match = HEREDOC.exec(out.slice(from))
    if (!match || match.index === undefined) break
    const at = from + match.index
    if (out[at + 2] === "<") { from = at + 3; continue }
    const delimiter = match[1] ?? match[2] ?? match[3] ?? ""
    const bodyStart = out.indexOf("\n", at + match[0].length)
    if (bodyStart === -1 || !delimiter) { from = at + match[0].length; continue }
    const lines = out.slice(bodyStart + 1).split("\n")
    let consumed = 0
    let closed = false
    for (const line of lines) {
      consumed += line.length + 1
      if (line.trim() === delimiter) { closed = true; break }
    }
    const bodyEnd = closed ? Math.min(bodyStart + consumed, out.length) : out.length
    out = out.slice(0, bodyStart) + out.slice(bodyEnd)
    from = at
  }
  return out
}

function words(input: string): string[] {
  const result: string[] = []
  let word = ""
  let quote: '"' | "'" | null = null
  for (let i = 0; i < input.length; i++) {
    const ch = input[i]
    if (quote === null && ch === "\\") { word += ch + (input[++i] ?? ""); continue }
    if (quote === null && (ch === '"' || ch === "'")) { quote = ch; word += ch; continue }
    if (quote !== null && ch === quote) { quote = null; word += ch; continue }
    if (quote === null && (/\s/.test(ch) || BREAKS.has(ch))) {
      if (word) result.push(word)
      word = ""
      continue
    }
    word += ch
  }
  if (word) result.push(word)
  return result
}

function unquote(word: string): string {
  if (word.length >= 2 && (word[0] === '"' || word[0] === "'") && word.at(-1) === word[0]) {
    return word.slice(1, -1)
  }
  return word.replace(/\\(.)/g, "$1")
}

function firstWord(input: string): string { return words(input)[0] ?? "" }
function harmless(path: string): boolean { return DEVICES.has(path) || /^\/dev\/fd\/\d+$/.test(path) }

function commandSegments(raw: string): string[] {
  const cmd = stripHeredocBodies(raw)
  const cuts: Array<{at: number; length: number}> = []
  scan(cmd, (_ch, i, quoted) => {
    if (quoted) return
    for (const op of ["&&", "||", ";", "|", "\n"]) {
      if (!cmd.startsWith(op, i)) continue
      const prior = cuts.at(-1)
      if (!prior || i >= prior.at + prior.length) cuts.push({at: i, length: op.length})
      return
    }
  })
  const result: string[] = []
  let start = 0
  for (const cut of cuts) { result.push(cmd.slice(start, cut.at)); start = cut.at + cut.length }
  result.push(cmd.slice(start))
  return result.map(part => part.trim()).filter(Boolean)
}

export function detectWriteTargets(raw: string): ShellWrite[] {
  const cmd = stripHeredocBodies(raw)
  const writes: ShellWrite[] = []
  const redirects: Array<{at: number; kind: WriteKind}> = []

  scan(cmd, (ch, i, quoted) => {
    if (quoted || ch !== ">" || cmd[i - 1] === ">") return
    const append = cmd[i + 1] === ">"
    redirects.push({at: i + (append ? 2 : 1), kind: append ? "append" : "redirect"})
  })
  for (const redirect of redirects) {
    const rest = cmd.slice(redirect.at)
    if (rest.trimStart().startsWith("(") || rest.startsWith("&")) continue
    const path = unquote(firstWord(rest))
    if (path && !path.startsWith("&")) writes.push({path, kind: redirect.kind})
  }

  for (const segment of commandSegments(cmd)) {
    const parts = words(segment)
    if (parts[0] === "tee") {
      const append = parts.slice(1).some(part => /^-[^-]*a/.test(part))
      for (const part of parts.slice(1)) {
        if (!part.startsWith("-")) writes.push({path: unquote(part), kind: append ? "append" : "tee"})
      }
    }
    if (parts[0] === "dd") {
      for (const part of parts.slice(1)) {
        if (part.startsWith("of=")) writes.push({path: unquote(part.slice(3)), kind: "dd"})
      }
    }
  }

  const seen = new Set<string>()
  return writes.filter(write => {
    if (!write.path || seen.has(write.path) || harmless(write.path)) return false
    seen.add(write.path)
    return true
  })
}

export function hasWriteRedirection(command: string): boolean {
  return detectWriteTargets(command).length > 0
}

// Adapted from little-coder's Apache-2.0 `_shared/shell-write.ts`.
// ponytail: this parses common model-emitted POSIX shell, not the full shell grammar.
// Use a shell AST if models begin emitting complex substitutions or arrays.
