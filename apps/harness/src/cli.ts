#!/usr/bin/env node
// tree-agent entry: a usage stub until task 204 lands the real CLI.

const USAGE = 'usage: tree-agent [flags] "<prompt>"';

process.stderr.write(`${USAGE}\n`);
process.exitCode = 1;
