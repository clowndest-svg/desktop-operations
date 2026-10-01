/**
 * Which of the two windows this page is.
 *
 * One bundle, two windows: the HUD and the desktop pet. Serving a second build
 * would double the packaging surface (and the ``npm run build`` race that already
 * broke one release) to ship a page that shares every module with this one.
 *
 * Read once at module scope rather than reactively: the URL cannot change under a
 * window that has no address bar.
 */
export type PageMode = 'hud' | 'pet'

export const pageMode: PageMode =
  new URLSearchParams(window.location.search).get('mode') === 'pet' ? 'pet' : 'hud'

/** True in the pet window, where there is no dashboard to draw or poll. */
export const isPet = pageMode === 'pet'
