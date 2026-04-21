import { z } from "zod";
import type { Tool } from "./types";

interface TodoItem {
  id: string;
  text: string;
  done: boolean;
}

// Process-lifetime list. Persistence + per-session isolation arrive at M4.
const todos: TodoItem[] = [];
let nextId = 1;

const schema = z.object({
  action: z
    .enum(["add", "list", "complete"])
    .describe("add: create a new item; list: show all; complete: mark done by id"),
  item: z.string().optional().describe("Required for action=add"),
  id: z.string().optional().describe("Required for action=complete"),
});

export const todoTool: Tool<z.infer<typeof schema>> = {
  name: "todo",
  description:
    "Simple in-memory todo list for session planning. Actions: add, list, complete. Items persist for the lifetime of this harness process.",
  schema,
  isReadOnly: false,
  isDestructive: false,
  async call({ action, item, id }) {
    if (action === "add") {
      if (!item) return { content: "action=add requires `item`", isError: true };
      const entry: TodoItem = { id: String(nextId++), text: item, done: false };
      todos.push(entry);
      return { content: `Added: [${entry.id}] ${entry.text}` };
    }
    if (action === "complete") {
      if (!id) return { content: "action=complete requires `id`", isError: true };
      const t = todos.find((x) => x.id === id);
      if (!t) return { content: `No todo with id=${id}`, isError: true };
      t.done = true;
      return { content: `Completed: [${t.id}] ${t.text}` };
    }
    if (todos.length === 0) return { content: "(no todos)" };
    return {
      content: todos.map((t) => `${t.done ? "[x]" : "[ ]"} [${t.id}] ${t.text}`).join("\n"),
    };
  },
};
