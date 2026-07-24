import { test, expect, type Page } from '@playwright/test'
import { getMockWebSocketScript } from './fixtures/mock-websocket'

/**
 * Visual Snapshot Tests for OpenOPC Office UI
 *
 * Covers the four main views:
 * - Dashboard (儀表盤)
 * - Chat / Workspace (工作區)
 * - Kanban (看板)
 * - Office (辦公室 / Phaser game view)
 *
 * These tests use a mocked WebSocket to provide deterministic fixture data,
 * ensuring screenshots are stable across runs.
 *
 * To update baselines after intentional UI changes:
 *   npm run test:visual:update
 */

/** Wait for the app to finish initial render and WebSocket fixture delivery */
async function waitForAppReady(page: Page): Promise<void> {
  // Wait for the topbar navigation to appear (app shell loaded)
  await page.waitForSelector('.page-nav', { timeout: 15_000 })
  // Allow fixture WebSocket messages to be delivered and processed
  await page.waitForTimeout(500)
  // Wait for any CSS transitions/animations to settle
  await page.waitForTimeout(300)
}

/** Navigate to a specific page via the top nav buttons */
async function navigateToPage(page: Page, pageIndex: number): Promise<void> {
  const buttons = page.locator('.page-nav-btn')
  await buttons.nth(pageIndex).click()
  await page.waitForTimeout(400)
}

test.describe('Visual Snapshots', () => {
  test.beforeEach(async ({ page }) => {
    // Inject mock WebSocket BEFORE app loads
    await page.addInitScript(getMockWebSocketScript())
    await page.goto('/')
    await waitForAppReady(page)
  })

  test('Dashboard view renders correctly', async ({ page }) => {
    // Dashboard is the 2nd nav button (index 1)
    await navigateToPage(page, 1)

    // Wait for dashboard content to render
    await page.waitForSelector('.dashboard-page, [class*="dashboard"]', { timeout: 5_000 }).catch(() => {
      // Fallback: just wait for content to settle
    })
    await page.waitForTimeout(300)

    await expect(page).toHaveScreenshot('dashboard.png', {
      fullPage: false,
    })
  })

  test('Chat / Workspace view renders correctly', async ({ page }) => {
    // Workspace is the 1st nav button (index 0) — default page
    // Already on workspace by default, just ensure it's settled
    await page.waitForTimeout(300)

    await expect(page).toHaveScreenshot('workspace-chat.png', {
      fullPage: false,
    })
  })

  test('Kanban board renders correctly', async ({ page }) => {
    // Workspace view contains the kanban board
    // Wait for kanban content
    await page.waitForSelector('.kanban-board, .kanban-column, [class*="kanban"]', { timeout: 5_000 }).catch(() => {
      // Kanban may be in a sub-tab; try clicking kanban tab if available
    })
    await page.waitForTimeout(300)

    // Try to find and click a kanban view toggle if present
    const kanbanTab = page.locator('[class*="kanban-tab"], [data-view="kanban"], button:has-text("Kanban"), button:has-text("看板")')
    if (await kanbanTab.count() > 0) {
      await kanbanTab.first().click()
      await page.waitForTimeout(400)
    }

    await expect(page).toHaveScreenshot('kanban-board.png', {
      fullPage: false,
    })
  })

  test('Office view renders correctly', async ({ page }) => {
    // Office is the 3rd nav button (index 2)
    await navigateToPage(page, 2)

    // Wait for Phaser game canvas or office container
    await page.waitForSelector('canvas, .office-container, [class*="office"], [class*="game"]', { timeout: 8_000 }).catch(() => {
      // Phaser may take longer to initialize
    })
    // Give Phaser extra time to render the scene
    await page.waitForTimeout(1_000)

    await expect(page).toHaveScreenshot('office-view.png', {
      fullPage: false,
    })
  })
})

test.describe('Visual Snapshots - Responsive', () => {
  test.use({ viewport: { width: 1024, height: 768 } })

  test.beforeEach(async ({ page }) => {
    await page.addInitScript(getMockWebSocketScript())
    await page.goto('/')
    await waitForAppReady(page)
  })

  test('Dashboard view at tablet width', async ({ page }) => {
    await navigateToPage(page, 1)
    await page.waitForTimeout(300)

    await expect(page).toHaveScreenshot('dashboard-tablet.png', {
      fullPage: false,
    })
  })

  test('Workspace view at tablet width', async ({ page }) => {
    await page.waitForTimeout(300)

    await expect(page).toHaveScreenshot('workspace-tablet.png', {
      fullPage: false,
    })
  })
})
