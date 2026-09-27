# visualizer/visualize.py

import matplotlib.pyplot as plt
from tabulate import tabulate

def visualize_result(columns, rows):
    # 1️⃣ Print as table
    print("\nRESULT TABLE:")
    print(tabulate(rows, headers=columns, tablefmt="grid"))

    # 2️⃣ Plot bar chart (if numeric result)
    if len(columns) == 2:
        labels = [row[0] for row in rows]
        values = [row[1] for row in rows]

        plt.figure()
        plt.bar(labels, values)
        plt.xlabel(columns[0])
        plt.ylabel(columns[1])
        plt.title("Query Result Visualization")
        plt.show()
