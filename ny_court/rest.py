
    async def set_date_value(self, field_name, date_string):
        """
        field_name: 'fromFileDate' or 'toFileDate'
        date_string: 'MM/DD/YYYY' format (e.g., '04/20/2026')
        """
        safe_name = json.dumps(field_name)
        safe_date = json.dumps(date_string)

        js_code = f"""
           (function() {{
               var el = document.querySelector('input[name=' + {safe_name} + ']');
               if (!el) return "NOT_FOUND";

               el.value = {safe_date};

               // Trigger events so the datepicker/validator sees the change
               el.dispatchEvent(new Event('focus', {{ bubbles: true }}));
               el.dispatchEvent(new Event('input', {{ bubbles: true }}));
               el.dispatchEvent(new Event('change', {{ bubbles: true }}));
               el.dispatchEvent(new Event('blur', {{ bubbles: true }}));

               return "SUCCESS";
           }})();
           """
        return await self.evaluate(js_code)


    async def scrape_court_table(self):
        js_extract = """
            (() => {
                const parseAmount = (str) => {
                    // Removes currency symbols, commas, and spaces to allow math comparison
                    return parseFloat(str.replace(/[$,\\s]/g, '')) || 0;
                };

                const rows = Array.from(document.querySelectorAll('table.dataList tbody tr'));

                return rows.map(row => {
                    const cells = Array.from(row.querySelectorAll('td'));
                    if (cells.length < 8) return null;

                    // The 'Money' is in the last td (index 7)
                    const rawAmount = cells[7].innerText.trim();
                    const numericAmount = parseAmount(rawAmount);

                    // Range Filter: $350,000 < x < $3,000,000
                    if (numericAmount > 350000 && numericAmount < 3000000) {
                        // The 'Name' and 'Link' are in the third td (index 2)
                        const thirdTd = cells[2];
                        const thirdTdAnchor = thirdTd.querySelector('a');

                        return {
                            control_number:   cells[0].innerText.trim(),
                            index_number:     cells[1].innerText.trim(),
                            debtor_name:      thirdTd.innerText.trim(),
                            debtor_link:      thirdTdAnchor ? thirdTdAnchor.href : null,
                            creditor_name:    cells[3].innerText.trim(),
                            book_type:        cells[4].innerText.trim(),
                            docket_date:      cells[5].innerText.trim(),
                            debtor_address:   cells[6].innerText.trim(),
                            amount_string:    rawAmount,
                            amount_numeric:   numericAmount
                        };
                    }
                    return null;
                }).filter(item => item !== null);
            })()
            """
        return await self.evaluate(js_extract)

    async def click_page(self, page_number):
        """Finds and clicks a specific page number in the navList."""
        self.logger.info(f"Attempting to navigate to page {page_number}...")

        # JS to find the link that contains the exact page number text
        js_click = f"""
        (() => {{
            const links = Array.from(document.querySelectorAll('table.navList a'));
            const target = links.find(a => a.innerText.trim() === "{page_number}");
            if (target && target.href) {{
                target.click();
                return true;
            }}
            return false;
        }})()
        """
        success = await self.evaluate(js_click)
        if success:
            # Wait for the table to refresh with new data
            await asyncio.sleep(2)
            await self.wait_for_results()
        return success

    async def get_total_pages(self):
        """Checks the navList to see how many pages _exist."""
        js_pages = """
        (() => {
            const pages = Array.from(document.querySelectorAll('table.navList span'))
                               .map(s => parseInt(s.innerText.trim()))
                               .filter(n => !isNaN(n));
            return pages.length > 0 ? Math.max(...pages) : 1;
        })()
        """
        return await self.evaluate(js_pages)

    async def scrape_block_lot(self):
        js_block_lot = """
        (() => {
            const table = document.querySelector('table[summary*="Block/Lots"]');
            if (!table) return null;

            const firstRow = table.querySelector('tbody tr');
            if (!firstRow) return null;

            const cells = firstRow.querySelectorAll('td');

            if (cells.length >= 3) {
                return {
                    "block_lot": `${cells[0].innerText.trim()} & ${cells[1].innerText.trim()}`,
                    "property_address": cells[2].innerText.trim()
                };
            }
            return null;
        })()
        """
        return await self.evaluate(js_block_lot)

if __name__ == '__main__':

    api_key = "replace this"
    url = "https://iapps.courts.state.ny.us/webccos/newyorkcc/countyFilingSearch"
    sitekey = '6LfmfjYUAAAAAMujuZ5wPlqjGqVYr7Ie4okh5aF-'
    task_type = "RecaptchaV2TaskProxyless"

    async def main():
        async with Small_Zombie(proxy=False,  user_dir=None, chrome_path=None, port=3000,headless=False) as driver:
            endpoint = driver.find_browser_ws_endpoint()
            tab = await driver.create_a_tab()
            await driver.navigate(url, wait_for_load=20)
            await asyncio.sleep(5)

            try:
                # Using your solver logic
                token = await driver.twocaptcha_task(api_key, task_type, url, sitekey)
                print(f"Token received: {token[:20]}...")

                # Inject the token
                # Note: We set value, innerHTML, AND make it visible to be safe

                injection_js = f"""
                            var target = document.getElementById('g-recaptcha-response');
                            target.style.display = 'block';
                            target.value = '{token}';
                            target.innerHTML = '{token}';
                        """
                await driver.evaluate(injection_js)
                print("Token injected successfully.")

                # Click the submit/search button
                # You'll need to verify the actual selector for the 'Search' button
                # await driver.evaluate("document.querySelector('input[type=\"submit\"]').click();")

            except Exception as e:
                print(f"Error during CAPTCHA solving: {e}")

            await driver.select_by_value("select[name='book']", "2")
            await driver.set_date_value("fromFileDate", "04/23/2026")
            await driver.set_date_value("toFileDate", "04/30/2026")
            await driver.evaluate("document.querySelector(\"input[name='btnSearchCountyFiling']\").click();")

            # 1. Verify we are on the results page before scraping
            if await driver.wait_for_results(timeout=20):
                all_matching_data = []
                total_pages = await driver.get_total_pages()
                driver.logger.info(f"Detected {total_pages} pages of results.")

                for page_num in range(1, total_pages + 1):
                    driver.logger.info(f"Processing Page {page_num}...")
                    page_data = await driver.scrape_court_table()

                    if page_data:
                        driver.logger.info(f"Found {len(page_data)} matches on page {page_num}.")

                        for row in page_data:
                            detail_url = row["debtor_link"]
                            if detail_url:
                                driver.logger.info(f"Visiting detail page for: {row['debtor_name']}")

                                # Navigate to the detail page
                                await driver.navigate(detail_url)

                                found = await driver.wait_for_selector('table[summary*="Block/Lots"]', timeout=10)

                                if found:
                                    # result is now a dict: {"block_lot": "...", "property_address": "..."}
                                    result = await driver.scrape_block_lot()
                                    if result:
                                        row["block & lot"] = result["block_lot"]
                                        row["property address"] = result["property_address"]
                                    else:
                                        row["block & lot"] = "N/A"
                                        row["property address"] = "N/A"
                                else:
                                    row["block & lot"] = "Not Found"
                                    row["property address"] = "Not Found"

                                # Go back to the main search results
                                all_matching_data.extend(page_data)
                                await driver.click(selector="input[name='btnBack']")
                                await driver.wait_for_results(timeout=10)
                                await asyncio.sleep(4)

                    # 2. If there are more pages, click the next one
                    if page_num < total_pages:
                        next_page = page_num + 1
                        success = await driver.click_page(next_page)
                        if not success:
                            driver.logger.warning(f"Could not click page {next_page}. Stopping.")
                            break
                #
                # # Final Output
                print(f"Total results across all pages: {len(all_matching_data)}")
                for row in all_matching_data:
                    print(row)

                if all_matching_data:
                    # 1. Convert to DataFrame
                    df_raw = pd.DataFrame(all_matching_data)

                    # 2. Map and Rename columns to your new format
                    # debtor_address is the "Owner's Address" (often empty in the list view)
                    # property address is the one we scraped from the detail page
                    column_mapping = {
                        "block & lot": "Block & Lot",
                        "property address": "Address",
                        "debtor_name": "Owner",
                        "debtor_address": "Owner's Address",
                        "creditor_name": "Lienor",
                        "docket_date": "DocketingDate",
                        "amount_string": "Amount"
                    }

                    # Rename and select only the columns you want
                    df = df_raw.rename(columns=column_mapping)
                    df = df[["Block & Lot", "Address", "Owner", "Owner's Address", "Lienor", "DocketingDate", "Amount"]]

                    # --- EXCEL EXPORT ---
                    today_str = datetime.now().strftime("%Y-%m-%d")
                    excel_filename = f"Court_Results_{today_str}.xlsx"
                    df.to_excel(excel_filename, index=False, engine='openpyxl')
                    driver.logger.info(f"Excel saved: {excel_filename}")

                else:
                    driver.logger.warning("No rows matched your criteria. Files not created.")

    asyncio.run(main())












