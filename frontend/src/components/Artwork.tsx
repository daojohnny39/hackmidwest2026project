import type { ReactNode } from 'react'
export function Icon({ name, className = '' }: { name: string; className?: string }) {
  const paths: Record<string, ReactNode> = {
    arrow: <path d="M5 12h14M13 6l6 6-6 6" />,
    camera: <><path d="m8 6 1.5-2h5L16 6h4v14H4V6Z" /><circle cx="12" cy="12.5" r="3.5" /></>,
    shield: <><path d="M12 3 4 6v6c0 4 8 9 8 9s8-5 8-9V6Z" /><path d="m8.5 12 2.5 2.5 4.5-5" /></>,
    globe: <><circle cx="12" cy="12" r="9" /><ellipse cx="12" cy="12" rx="4" ry="9" /><path d="M3 12h18M5 6.5h14M5 17.5h14" /></>,
    close: <path d="m6 6 12 12M6 18 18 6" />,
    check: <path d="m5 12 4 4L19 6" />,
    plus: <path d="M12 5v14M5 12h14" />,
    stop: <rect x="6" y="6" width="12" height="12" rx="1" />,
    reset: <path d="M4 10a8 8 0 1 1 1 7M4 4v6h6" />,
    link: <path d="m10 13 4-4M8 15l-1 1a3.5 3.5 0 0 1-5-5l4-4a3.5 3.5 0 0 1 5 0M13 9l1-1a3.5 3.5 0 0 1 5 5l-4 4a3.5 3.5 0 0 1-5 0" />,
  }
  return <svg className={`icon ${className}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] ?? paths.arrow}</svg>
}
export function Mark() {
  return <svg className="brand-mark" viewBox="0 0 40 40" fill="none" aria-hidden="true"><path d="M13 5H5v8M27 5h8v8M35 27v8h-8M13 35H5v-8" stroke="currentColor" strokeWidth="2.2" /><circle cx="20" cy="20" r="9" stroke="currentColor" strokeWidth="1.6" /><path d="M14 16h12M17 16v10M23 16v10" stroke="currentColor" strokeWidth="1.6" /></svg>
}
export function FaceArtwork() {
  return <svg className="face-art" viewBox="0 0 400 360" fill="none" aria-hidden="true">
    <defs><radialGradient id="face-glow"><stop stopColor="#b7c9a2" stopOpacity=".1" /><stop offset="1" stopColor="#b7c9a2" stopOpacity="0" /></radialGradient></defs>
    <circle cx="200" cy="172" r="155" fill="url(#face-glow)" />
    <g stroke="currentColor" strokeWidth=".7"><ellipse cx="200" cy="160" rx="80" ry="108" opacity=".65" />{[30, 47, 64].map(r => <ellipse key={r} cx="200" cy="160" rx={r} ry="108" opacity=".27" />)}{[88, 112, 136, 160, 184, 208, 232].map((y, i) => <ellipse key={y} cx="200" cy={y} rx={[54, 70, 78, 80, 78, 70, 54][i]} ry="12" opacity=".25" />)}<path d="M200 52v216M120 160h160M150 244l-7 35-70 27-16 28h286l-16-28-70-27-7-35" opacity=".35" /><path d="m162 143 14-4 10 6m28 0 10-6 14 4M200 144l-9 40h18M182 213q18 9 36 0" opacity=".65" /><path d="M93 83V65h20M287 65h20v18M93 237v18h20M287 255h20v-18" strokeWidth="1.7" /><path d="M57 174h44m198 0h44M200 30v-12M200 298v13" opacity=".4" /></g>
    <g fill="currentColor">{[[200,52],[200,268],[120,160],[280,160],[176,141],[224,141],[200,183],[182,213],[218,213]].map(([cx,cy],i)=><circle key={i} cx={cx} cy={cy} r={i<4?2.5:1.8} />)}</g>
  </svg>
}
export function Orbit() {
  return <svg className="orbit-art" viewBox="0 0 280 230" fill="none" aria-hidden="true"><g stroke="currentColor" strokeWidth=".65"><circle cx="140" cy="117" r="74" /><ellipse cx="140" cy="117" rx="31" ry="74" /><ellipse cx="140" cy="117" rx="57" ry="74" /><ellipse cx="140" cy="117" rx="74" ry="26" /><path d="M140 43v148M66 117h148M77 78h126M77 156h126" /><ellipse cx="140" cy="117" rx="123" ry="40" transform="rotate(-30 140 117)" /></g><g fill="currentColor"><circle cx="49" cy="168" r="5" /><circle cx="224" cy="67" r="4" /><circle cx="140" cy="117" r="3" /></g><path d="m240 162 9 5v11l-9 5-9-5v-11ZM31 43l8 4v10l-8 4-8-4V47Z" stroke="currentColor" /></svg>
}
