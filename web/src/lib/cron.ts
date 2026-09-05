/** Human preview for the common 5-field cron shapes. Anything else is shown verbatim. */

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

export function isValidCron(expr: string): boolean {
  const f = expr.trim().split(/\s+/);
  if (f.length !== 5) return false;
  const re = /^(\*|\d+)(-\d+)?(\/\d+)?(,(\*|\d+)(-\d+)?(\/\d+)?)*$/;
  return f.every((x) => re.test(x));
}

function pad(n: string): string {
  return n.padStart(2, '0');
}

function dayList(spec: string): string | null {
  if (spec === '1-5') return 'weekdays';
  if (spec === '0,6' || spec === '6,0') return 'weekends';
  const parts = spec.split(',');
  const names = parts.map((p) => {
    const n = Number(p);
    return Number.isInteger(n) && n >= 0 && n <= 7 ? DAYS[n % 7] : null;
  });
  return names.every((x) => x !== null) ? names.join(', ') : null;
}

export function describeCron(expr: string): string {
  if (!isValidCron(expr)) return 'Not a valid 5-field cron expression';
  const [min = '*', hour = '*', dom = '*', mon = '*', dow = '*'] = expr.trim().split(/\s+/);
  const everyMin = /^\*\/(\d+)$/.exec(min);
  if (everyMin && hour === '*' && dom === '*' && mon === '*' && dow === '*') return `Every ${everyMin[1]} minutes`;
  if (min === '*' && hour === '*' && dom === '*' && mon === '*' && dow === '*') return 'Every minute';
  const everyHour = /^\*\/(\d+)$/.exec(hour);
  if (/^\d+$/.test(min) && everyHour && dom === '*' && mon === '*' && dow === '*') return `Every ${everyHour[1]} hours at :${pad(min)}`;
  if (/^\d+$/.test(min) && hour === '*' && dom === '*' && mon === '*' && dow === '*') return `Every hour at :${pad(min)}`;
  if (/^\d+$/.test(min) && /^\d+$/.test(hour)) {
    const time = `${pad(hour)}:${pad(min)}`;
    if (dom === '*' && mon === '*' && dow === '*') return `Every day at ${time}`;
    if (dom === '*' && mon === '*') {
      const days = dayList(dow);
      if (days) return `${days === 'weekdays' || days === 'weekends' ? days[0]?.toUpperCase() + days.slice(1) : days} at ${time}`;
    }
    if (/^\d+$/.test(dom) && mon === '*' && dow === '*') return `Monthly on day ${dom} at ${time}`;
  }
  return `cron ${expr.trim()}`;
}
