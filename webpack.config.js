const path = require('path');
const MiniCssExtractPlugin = require('mini-css-extract-plugin');

module.exports = {
  entry: './assets/js/application.js',
  output: {
    filename: 'bundle.js',
    path: path.resolve(__dirname, 'assets/js'),
  },
  plugins: [
    // Emit compiled CSS (Tailwind + Prism theme) as a real stylesheet so pages
    // don't flash unstyled while the JS bundle loads.
    new MiniCssExtractPlugin({ filename: '../css/app.css' }),
  ],
  module: {
    rules: [
      {
        test: /\.js$/,
        exclude: /node_modules/,
        use: {
          loader: 'babel-loader',
          options: {
            presets: ['@babel/preset-env']
          }
        }
      },
      {
        test: /\.css$/,
        use: [MiniCssExtractPlugin.loader, 'css-loader', 'postcss-loader']
      }
    ]
  }
};
