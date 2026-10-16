// Panel chrome shared by every grid item: draggable header (the grid's
// draggableHandle), title, optional ⓘ help popover, close button, scrollable body.
import { useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

// ⓘ button + hover/click help popover. The popover is portalled to <body> at a
// fixed position computed from the button, so the panel's `overflow:hidden` (and
// any short panel) can never clip it. Hover or keyboard-focus opens it; click
// toggles (pins) it for touch. The popover is non-interactive (pointer-events:
// none) so moving the cursor off the button always closes it — no hover gap.
function HelpButton({ text }: { text: string }) {
  const btnRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number }>({ top: 0, left: 0 });

  const place = () => {
    const r = btnRef.current?.getBoundingClientRect();
    if (!r) return;
    const width = 300;
    setPos({
      top: r.bottom + 5,
      left: Math.max(8, Math.min(r.left, window.innerWidth - width - 8)),
    });
  };

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        className="panel-help"
        // No native `title`: the browser tooltip would stack a second box over
        // our portalled popover and obscure it. aria-label keeps the a11y name.
        aria-label="What this widget shows"
        onMouseEnter={() => {
          place();
          setOpen(true);
        }}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => {
          place();
          setOpen(true);
        }}
        onBlur={() => setOpen(false)}
        onClick={(e) => {
          e.stopPropagation();
          place();
          setOpen((o) => !o);
        }}
      >
        ⓘ
      </button>
      {open &&
        createPortal(
          <div className="help-pop" style={{ top: pos.top, left: pos.left }} role="tooltip">
            {text}
          </div>,
          document.body,
        )}
    </>
  );
}

export function Panel({
  title,
  help,
  onRemove,
  children,
}: {
  title: string;
  help?: string;
  onRemove: () => void;
  children: ReactNode;
}) {
  return (
    <div className="panel">
      <div className="panel-header">
        <span className="panel-title" style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{title}</span>
        <span className="spacer" />
        {help && <HelpButton text={help} />}
        <button className="panel-close" title="Remove" aria-label="Close panel" onClick={onRemove}>
          ×
        </button>
      </div>
      <div className="panel-body">{children}</div>
    </div>
  );
}
