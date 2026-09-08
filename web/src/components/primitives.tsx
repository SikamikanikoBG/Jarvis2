import { useEffect, useLayoutEffect, useRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { formatRelative } from '../lib/format';
import { Icon, type IconName } from './Icon';
import { useTicker } from './useTicker';

// ---- IconButton ---------------------------------------------------------------------------

interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  icon: IconName;
  label: string;
  size?: 'sm' | 'md';
  active?: boolean;
}

export function IconButton({ icon, label, size = 'md', active, className, ...rest }: IconButtonProps) {
  const cls = ['icon-btn', size === 'sm' ? 'sm' : '', active ? 'active' : '', className ?? ''].filter(Boolean).join(' ');
  return (
    <button type="button" className={cls} aria-label={label} title={label} {...rest}>
      <Icon name={icon} size={size === 'sm' ? 16 : 19} />
    </button>
  );
}

// ---- Switch -------------------------------------------------------------------------------

interface SwitchProps {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  label: string;
}

export function Switch({ checked, onChange, disabled, label }: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className="switch"
      disabled={disabled}
      onClick={() => onChange(!checked)}
    />
  );
}

// ---- Menu (small popover) -----------------------------------------------------------------

export interface MenuItem {
  label: string;
  icon?: IconName;
  danger?: boolean;
  /** Shown but not clickable — for the one-line explanation of an empty submenu. */
  disabled?: boolean;
  /** Leaves the menu open, for an item that swaps the menu's contents (a submenu). */
  keepOpen?: boolean;
  onSelect: () => void;
}

interface MenuProps {
  items: MenuItem[];
  onClose: () => void;
  anchor: HTMLElement | null;
}

export function Menu({ items, onClose, anchor }: MenuProps) {
  const ref = useRef<HTMLDivElement>(null);

  // Position next to the anchor, flipping above when there is no room below.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!anchor || !el) return;
    const r = anchor.getBoundingClientRect();
    const width = el.offsetWidth || 180;
    const height = el.offsetHeight || 120;
    const left = Math.max(8, Math.min(r.right - width, window.innerWidth - width - 8));
    const top = r.bottom + height + 8 > window.innerHeight ? r.top - height - 4 : r.bottom + 4;
    el.style.top = `${top}px`;
    el.style.left = `${left}px`;
  }, [anchor]);

  useEffect(() => {
    const onDoc = (e: MouseEvent | TouchEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node) && e.target !== anchor) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('touchstart', onDoc);
    document.addEventListener('keydown', onKey);
    ref.current?.querySelector<HTMLElement>('button')?.focus();
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('touchstart', onDoc);
      document.removeEventListener('keydown', onKey);
    };
  }, [anchor, onClose]);

  return (
    <div ref={ref} className="menu" role="menu" style={{ position: 'fixed', top: -9999, left: -9999 }}>
      {items.map((it) => (
        <button
          key={it.label}
          type="button"
          role="menuitem"
          className={it.danger ? 'menu-item danger' : 'menu-item'}
          disabled={it.disabled}
          onClick={() => {
            if (!it.keepOpen) onClose();
            it.onSelect();
          }}
        >
          {it.icon && <Icon name={it.icon} size={16} />}
          {it.label}
        </button>
      ))}
    </div>
  );
}

// ---- InlineConfirm ------------------------------------------------------------------------

interface InlineConfirmProps {
  text: string;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
  danger?: boolean;
}

export function InlineConfirm({ text, confirmLabel, onConfirm, onCancel, danger }: InlineConfirmProps) {
  const ref = useRef<HTMLButtonElement>(null);
  useEffect(() => ref.current?.focus(), []);
  return (
    <div className="inline-confirm" role="alertdialog" aria-label={text}>
      <span className="truncate">{text}</span>
      <button ref={ref} type="button" className={`btn btn-sm ${danger ? 'btn-danger' : 'btn-primary'}`} onClick={onConfirm}>
        {confirmLabel}
      </button>
      <button type="button" className="btn btn-sm btn-ghost" onClick={onCancel}>
        Cancel
      </button>
    </div>
  );
}

// ---- Drawer / Sheet -----------------------------------------------------------------------

interface DrawerProps {
  side: 'left' | 'sheet';
  onClose: () => void;
  label: string;
  children: ReactNode;
}

export function Drawer({ side, onClose, label, children }: DrawerProps) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <>
      <div className="backdrop" onClick={onClose} aria-hidden="true" />
      <div className={`drawer ${side === 'left' ? 'drawer-left' : 'drawer-sheet'}`} role="dialog" aria-modal="true" aria-label={label}>
        {side === 'sheet' && <div className="drawer-handle" />}
        {children}
      </div>
    </>
  );
}

// ---- Time ---------------------------------------------------------------------------------

export function RelativeTime({ ts }: { ts: string }) {
  const now = useTicker(30_000);
  return (
    <time dateTime={ts} title={new Date(ts).toLocaleString()}>
      {formatRelative(ts, now)}
    </time>
  );
}
