/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./chat/templates/**/*.html",
    "./aistarterkit/templates/**/*.html",
    "./assets/js/**/*.js",
    "./chat/forms.py",  // widget class strings live here
  ],
  theme: { extend: {} },
  plugins: [],
};
