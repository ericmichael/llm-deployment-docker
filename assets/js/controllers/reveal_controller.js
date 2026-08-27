import { Controller } from "stimulus"

// Toggles a secret input between masked (password) and visible (text).
export default class extends Controller {
  static targets = ["input", "icon", "label"]

  toggle(event) {
    event.preventDefault()
    const hidden = this.inputTarget.type === "password"
    this.inputTarget.type = hidden ? "text" : "password"
    if (this.hasIconTarget) this.iconTarget.className = hidden ? "fas fa-eye-slash" : "fas fa-eye"
    if (this.hasLabelTarget) this.labelTarget.textContent = hidden ? "Hide" : "Show"
  }
}
