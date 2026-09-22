"""Signal extraction. One SQL pass produces the same feature row for any (card_id, txn_id),
so the features the agent reasons over on the 20 exam cases are literally the features
validated against the 4665 confirmed / 900 cleared closed cases."""

FEATURE_SQL = """
WITH anchor AS (
  SELECT a.key_id, t.*
  FROM anchors a JOIN tx t ON t.txn_id = a.txn_id
),
-- how many distinct cards each device profile touches overall: a profile on
-- hundreds of cards is background noise, not a link.
dev_pop AS (
  SELECT device_profile, count(DISTINCT card_id) AS dev_cards
  FROM tx WHERE device_profile IS NOT NULL GROUP BY device_profile
)
SELECT
  a.key_id, a.txn_id, a.card_id, a.customer_id, a.ts, a.amount, a.product_cd,
  a.channel, a.risk_score, a.addr1, a.p_email, a.device_profile, a.device_info, a.id_15, a.id_23,
  p.n_txns, p.amt_median, p.amt_p95, p.amt_max, p.online_share, p.n_regions, p.n_devices,

  a.amount / nullif(p.amt_median, 0)                              AS amt_vs_median,
  CASE WHEN a.amount > p.amt_p95 THEN 1 ELSE 0 END                AS amt_over_p95,

  -- has this card ever been billed in this region before now?
  (SELECT count(*) FROM tx x WHERE x.card_id = a.card_id
     AND x.addr1 IS NOT DISTINCT FROM a.addr1 AND x.ts < a.ts)    AS prior_in_region,
  -- ever used this product code before?
  (SELECT count(*) FROM tx x WHERE x.card_id = a.card_id
     AND x.product_cd = a.product_cd AND x.ts < a.ts)             AS prior_product,
  -- ever used this email domain before?
  (SELECT count(*) FROM tx x WHERE x.card_id = a.card_id
     AND x.p_email IS NOT DISTINCT FROM a.p_email AND x.ts < a.ts) AS prior_email,
  -- same amount within 2%: the recurring-charge tell (policy R7)
  (SELECT count(*) FROM tx x WHERE x.card_id = a.card_id
     AND abs(x.amount - a.amount) <= 0.02 * a.amount)             AS same_amount_n,
  -- R7 is 'same merchant, same amount, monthly', so cadence matters, not raw count:
  -- distinct calendar months in which this exact amount was charged to this card.
  (SELECT count(DISTINCT date_trunc('month', x.ts)) FROM tx x WHERE x.card_id = a.card_id
     AND abs(x.amount - a.amount) <= 0.02 * a.amount
     AND x.product_cd = a.product_cd)                             AS same_amount_months,
  -- median gap in days between charges of this exact amount under this product code.
  -- A subscription lands near 30; a coincidence on a high-volume card lands near 0.
  (SELECT median(gap) FROM (
     SELECT date_diff('day', lag(x.ts) OVER (ORDER BY x.ts), x.ts) AS gap
     FROM tx x WHERE x.card_id = a.card_id AND x.product_cd = a.product_cd
       AND abs(x.amount - a.amount) <= 0.02 * a.amount) g
   WHERE gap IS NOT NULL)                                         AS same_amount_gap_days,
  -- burst: transactions on this card in the 48h ending at the anchor
  (SELECT count(*) FROM tx x WHERE x.card_id = a.card_id
     AND x.ts BETWEEN a.ts - INTERVAL 48 HOUR AND a.ts)           AS burst_48h,
  -- card-testing shape: sub-$5 online auths in the 24h before the anchor
  (SELECT count(*) FROM tx x WHERE x.card_id = a.card_id AND x.channel = 'online'
     AND x.amount < 5 AND x.ts BETWEEN a.ts - INTERVAL 24 HOUR AND a.ts) AS small_auths_24h,
  -- device reach and whether that device already produced confirmed fraud
  coalesce(d.dev_cards, 0)                                        AS dev_cards,
  -- how many of the 4 profile components are 'unknown': a profile like
  -- 'unknown | unknown | chrome 66.0 | unknown' describes a browser, not a machine.
  (length(coalesce(a.device_profile,'')) -
   length(replace(coalesce(a.device_profile,''),'unknown',''))) / 7 AS dev_unknowns,
  -- DeviceInfo naming actual hardware (e.g. 'SM-G935F Build/NRD90M') rather than a
  -- platform word. A named handset shared across dozens of cards is a ring; a common
  -- desktop configuration shared across dozens is just Windows.
  CASE WHEN a.device_info IS NOT NULL
        AND a.device_info NOT IN ('Windows','MacOS','iOS Device','Trident/7.0','Linux','other')
        AND a.device_info NOT LIKE 'unknown%' THEN 1 ELSE 0 END   AS dev_named_hw,
  CASE WHEN a.device_profile IS NULL THEN 0
       WHEN a.device_profile LIKE 'unknown | unknown | unknown%' THEN 0
       ELSE 1 END                                                 AS dev_specific,
  CASE WHEN a.id_15 = 'New' THEN 1 ELSE 0 END                     AS dev_new,
  CASE WHEN a.id_23 IS NOT NULL AND a.id_23 <> 'IP_PROXY:TRANSPARENT' THEN 1 ELSE 0 END AS proxy,
  -- channel out of character for this card
  CASE WHEN a.channel = 'online' AND p.online_share < 0.1 THEN 1
       WHEN a.channel = 'in_person' AND p.online_share > 0.9 THEN 1 ELSE 0 END AS channel_odd,
  -- M flags: M1..M9 are match flags (name/address match etc). Count the F's.
  (CASE WHEN a.M1='F' THEN 1 ELSE 0 END + CASE WHEN a.M2='F' THEN 1 ELSE 0 END +
   CASE WHEN a.M3='F' THEN 1 ELSE 0 END + CASE WHEN a.M5='F' THEN 1 ELSE 0 END +
   CASE WHEN a.M6='F' THEN 1 ELSE 0 END + CASE WHEN a.M7='F' THEN 1 ELSE 0 END +
   CASE WHEN a.M8='F' THEN 1 ELSE 0 END + CASE WHEN a.M9='F' THEN 1 ELSE 0 END) AS m_false_n
FROM anchor a
JOIN card_profile p ON p.card_id = a.card_id
LEFT JOIN dev_pop d ON d.device_profile = a.device_profile
"""
