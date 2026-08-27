import { Application } from "stimulus"
import Prism from 'prismjs';

import ClipboardController from "./controllers/clipboard_controller"
import RevealController from "./controllers/reveal_controller"

const application = Application.start();
// App controllers are registered explicitly (a require.context on the
// directory breaks `npm run build` when it is empty on a fresh checkout).
application.register('clipboard', ClipboardController)
application.register('reveal', RevealController)

// Import and register all TailwindCSS Components or just the ones you need
import { Alert, Autosave, ColorPreview, Dropdown, Modal, Tabs, Popover, Toggle, Slideover } from "tailwindcss-stimulus-components"
application.register('alert', Alert)
application.register('autosave', Autosave)
application.register('color-preview', ColorPreview)
application.register('dropdown', Dropdown)
application.register('modal', Modal)
application.register('popover', Popover)
application.register('slideover', Slideover)
application.register('tabs', Tabs)
application.register('toggle', Toggle)

document.addEventListener('DOMContentLoaded', (event) => {
    console.log("Hello World.")
    Prism.highlightAll();
});