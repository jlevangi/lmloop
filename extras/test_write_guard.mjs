import assert from "node:assert/strict"
import {mkdtempSync, writeFileSync} from "node:fs"
import {tmpdir} from "node:os"
import {join} from "node:path"
import test from "node:test"
import {detectWriteTargets} from "./_shared/write-guard-shell-write.ts"
import writeGuard from "./write-guard.ts"

function hook(cwd) {
  const handlers = []
  writeGuard({on(event, handler) { if (event === "tool_call") handlers.push(handler) }})
  return async (toolName, input) => {
    for (const handler of handlers) {
      const result = await handler({toolName, input}, {cwd})
      if (result?.block) return result
    }
  }
}

function repo() {
  const root = mkdtempSync(join(tmpdir(), "write-guard-"))
  writeFileSync(join(root, "existing.txt"), "keep this\n")
  return root
}

test("shell parser finds destructive writes without mistaking plumbing for files", () => {
  assert.deepEqual(detectWriteTargets("cat > existing.txt"), [{path: "existing.txt", kind: "redirect"}])
  assert.deepEqual(detectWriteTargets("cat <<'EOF' > existing.txt\na > b\nEOF"), [{path: "existing.txt", kind: "redirect"}])
  assert.deepEqual(detectWriteTargets("printf x | tee 'existing.txt'"), [{path: "existing.txt", kind: "tee"}])
  assert.deepEqual(detectWriteTargets("dd if=in of=existing.txt"), [{path: "existing.txt", kind: "dd"}])
  assert.deepEqual(detectWriteTargets("test -f x 2>/dev/null && echo ok >&2"), [])
  assert.deepEqual(detectWriteTargets("grep 'a > b' existing.txt"), [])
})

test("an unread existing file is blocked before write", async () => {
  const call = hook(repo())
  const result = await call("write", {path: "existing.txt", content: "replacement\n"})
  assert.equal(result?.block, true)
  assert.match(result.reason, /have not read/)
})

test("reading an existing small file permits intentional replacement", async () => {
  const root = repo()
  const call = hook(root)
  assert.equal(await call("read", {path: "existing.txt"}), undefined)
  assert.equal(await call("write", {path: "existing.txt", content: "replacement\n"}), undefined)
})

test("new and out-of-tree files are not this guard's business", async () => {
  const root = repo()
  const call = hook(root)
  assert.equal(await call("write", {path: "new.txt", content: "new\n"}), undefined)
  assert.equal(await call("write", {path: join(tmpdir(), "outside.txt"), content: "x\n"}), undefined)
})

test("shell overwrite of an unread file is blocked, including heredocs", async () => {
  const call = hook(repo())
  for (const command of [
    "cat > existing.txt <<'EOF'\nreplacement\nEOF",
    "printf replacement > existing.txt",
    "printf replacement | tee existing.txt",
    "dd if=/tmp/input of=existing.txt",
  ]) {
    const result = await call("bash", {command})
    assert.equal(result?.block, true, command)
  }
})

test("append does not destroy existing content and remains available", async () => {
  const call = hook(repo())
  assert.equal(await call("bash", {command: "printf more >> existing.txt"}), undefined)
  assert.equal(await call("bash", {command: "printf more | tee -a existing.txt"}), undefined)
})

test("shell aliases used by pi extensions receive the same guard", async () => {
  for (const toolName of ["bash", "Bash", "ShellSession", "ShellStart"]) {
    const call = hook(repo())
    const result = await call(toolName, {command: "cat > existing.txt"})
    assert.equal(result?.block, true, toolName)
  }
})
