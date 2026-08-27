import { Controller } from "stimulus"

// Copies `data-clipboard-text-value` (or the source input's value) and flashes feedback.
export default class extends Controller {
  static targets = ["source", "button"]
  static values = { text: String, success: { type: String, default: "Copied" } }

  async copy(event) {
    event.preventDefault()
    const text = this.textValue || (this.hasSourceTarget ? this.sourceTarget.value : "")
    if (!text) return
    try {
      await navigator.clipboard.writeText(text)
      this.flash(this.successValue)
    } catch (_) {
      if (this.hasSourceTarget) { this.sourceTarget.select(); document.execCommand("copy") }
      this.flash(this.successValue)
    }
  }

  flash(label) {
    const button = this.hasButtonTarget ? this.buttonTarget : event.currentTarget
    if (!button) return
    const original = button.innerHTML
    button.innerHTML = `<i class="fas fa-check mr-1"></i>${label}`
    button.classList.add("text-green-600")
    setTimeout(() => { button.innerHTML = original; button.classList.remove("text-green-600") }, 1500)
  }
}
