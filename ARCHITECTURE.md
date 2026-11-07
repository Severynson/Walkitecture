Backend endpoints:

- login
- (uplod DWG -> runs the process of Babylon.js code generation, writing it to the database for a particular DWG. We're going to use)
-

Modules:

## DWG is being read like in helper/read_dwg.py and is being transformed to descriptive JSON (lines, circles, isert, etc.) to define what elements are walls, what elements are windows, doors, and which are other complex 3-D objects such as furniture beside their shapes;

## JSON into the Babylon.js code convertor

- Walls turn into walls fairly easy - just add a constant height for walls (e.g. 10 inches). All the walls are the same height.
- Windows and dorframes/doors - just make all the doorframes 7 feet in height. Over it - add a wall of the same thickness as on the right and on the left of it. Windows must start on the height 2.5 feets, and end on the height of 6.5 feets.

### code which will produce 3-D mockups when it reaches frontend.

simple shapes such as lines, circles, arc, and lwpolyline which appear to be walls have to be generated automatically with the algorithmic part. When windows or doorframes/doors are present - 7 ft high )
