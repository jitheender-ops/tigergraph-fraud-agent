import { test, expect } from '@playwright/test';

// A case the agent asked its cardholder about, read from the API rather than hard-coded:
// which cases ask anything depends on the evidence, and that is allowed to change.
async function caseAskingTheCardholder(request: any): Promise<string> {
  const cases = await (await request.get('/api/cases')).json();
  const hit = cases.find((c: any) => c.source === 'cases' &&
    c.evidence_requests.some((q: any) => q.type === 'customer_validation' && q.simulated));
  expect(hit, 'some benchmark case asks the cardholder').toBeTruthy();
  return hit.case_id;
}

test('the queue loads and every case is listed', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('Investigation Desk')).toBeVisible();
  await expect(page.getByText(/^HHG-0\d\d$/)).toHaveCount(20);
});

test('a change without a token is refused, and says so', async ({ page, request }) => {
  const cid = await caseAskingTheCardholder(request);
  await page.goto('/');
  await page.getByText(cid, { exact: true }).first().click();
  await expect(page.getByText('Evidence Requested')).toBeVisible();
  await page.getByRole('button', { name: 'confirmed' }).first().click();
  await expect(page.getByRole('alert')).toContainText('X-Analyst-Token');
});

test('a recorded reply replaces the simulated one', async ({ page, request }) => {
  const cid = await caseAskingTheCardholder(request);
  await page.goto('/');
  await page.getByLabel('Analyst token').fill('e2e-analyst');
  await page.getByText(cid, { exact: true }).first().click();
  await page.getByRole('button', { name: 'confirmed' }).first().click();
  await expect(page.getByText(/^REPLY RECEIVED .*: confirmed$/)).toBeVisible({ timeout: 60_000 });
});

test('a live trigger opens a new case', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Analyst token').fill('e2e-analyst');
  await page.getByRole('button', { name: 'Live' }).click();
  await page.getByPlaceholder(/card id/).fill('C04172-K2');
  await page.getByPlaceholder('flagged transaction id').fill('3416383');
  await page.getByRole('combobox').last().selectOption('customer_report');
  await page.getByRole('button', { name: 'Investigate' }).click();
  await expect(page.getByText('API-3416383').first()).toBeVisible({ timeout: 60_000 });
  // the override picker lists every action too, so only a visible one counts
  await expect(page.locator(':text-is("BLOCK_ALL_CARDS"):visible').first()).toBeVisible();
});
