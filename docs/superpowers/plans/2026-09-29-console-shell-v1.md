# Console Shell v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the console shell match `haflow-flow-canvas-proposal.html` without changing workflow execution.

**Architecture:** Restyle and restructure the existing console template. Navigation calls the current dashboard, ops, controller, template, and archive functions. Flow nodes switch from X6 rect labels to one registered HTML shape.

**Tech Stack:** Python console template, vanilla JS, vendored X6 3.1.8 HTML shape, pytest DOM-contract tests.

## Global Constraints

- 实现已按确认的视觉契约落地；用户要求后以 PR 交付。
- Keep flow workbench hooks: flowCanvas, viewFlowBtn, renderFlowGraph, openTaskDrawer, executeControllerAction, dashButton, syncOpsUi two-way copy.
- Do not invent dry-run, version diff, or a node palette.
- Node title uses cleanStageLabel. Empty nodes say 尚未开始.

---

### Task 1: Shell contract test

**Files:**
- Create: `tests/test_console_shell.py`
- Modify: `tests/test_console_templates.py`

- [x] Add assertions for spaceSwitcher, crumbSection, nav groups, flow-card, and 读作.
- [x] Replace the removed panel-title snippet in the terminology test.
- [x] Run `pytest -q tests/test_console_shell.py` and confirm it fails before the template change.

### Task 2: Sidebar, top bar, canvas chrome

**Files:**
- Modify: `console/herdr_factory_console.py`

- [x] Replace the sidebar with brand, switcher, three groups, and footer.
- [x] Replace the top bar with the breadcrumb and the three workflow actions.
- [x] Move the view toggle and zoom into the canvas toolbar. Hide metrics, stages, and the bottom roster on the workbench via `data-view`.
- [x] Add `showShellView`, `paintCrumb`, and `toggleSpaceMenu`. Stop ops/dashboard from overwriting `#projects`.

### Task 3: Node cards and inspector

**Files:**
- Modify: `console/herdr_factory_console.py`

- [x] Register `flow-card` and render status, purpose, and counts from existing graph fields.
- [x] Rewrite the summary tab to 读作, checklist, and current tasks.
- [x] Re-run the shell, flow workbench, template, project, dashboard, and syntax tests.

### Task 4: Visual check

- [x] Serve the console on a free port and screenshot the workbench in headless Chrome.
- [x] Compare the shell against the proposal: switcher, groups, dotted canvas, inspector.
